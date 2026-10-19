"""Local settings page: which games and events reach each Discord channel.

    python3 -m store_monitor.settings [--state FILE] [--port 8740]

Opens a page on 127.0.0.1 that edits config.json in this checkout. The game list
comes from the monitor's state (downloaded from STATE_BUCKET with gcloud, or a
local --state file). Webhook URLs typed into the page go straight to Secret
Manager and GitHub secrets through gcloud and gh, never into config.json or a
file. Settings are read from ./setup.env (KEY=value lines) or the environment:
GCP_PROJECT, GITHUB_REPO, STATE_BUCKET and MONITOR_SERVICE_ACCOUNT.

Saving only writes config.json; commit and push it to main to deploy.
"""
import argparse
import json
import os
import secrets
import subprocess
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import discord
from .delivery import WEBHOOK_SECRET, discord_channels

PAGE = Path(__file__).with_name("settings.html")

# Event kinds the page offers, per store, with plain labels. Kinds not listed here still work in config.json.
EVENTS = {
    "apple": [
        ("waiting_for_review", "Waiting for review"), ("in_review", "In review"),
        ("approved", "Approved, awaiting release"), ("rejected", "Rejected"),
        ("published", "Live on the App Store"), ("rollout_paused", "Phased release paused"),
        ("rollout_resumed", "Phased release resumed"), ("rollout_changed", "Phased release progressed"),
        ("rollout_completed", "Phased release completed"), ("draft", "New version draft"),
        ("ready_for_review", "Ready for review"), ("review_withdrawn", "Withdrawn from review"),
        ("notes_updated", "Release notes edited"), ("release_build_changed", "Different build selected"),
        ("release_renamed", "Version renamed"), ("status_changed", "Other status changes"),
        ("build_processing", "Build uploaded, processing"), ("build_processed", "Build processed"),
        ("build_failed", "Build processing failed"), ("build_invalid", "Build invalid"),
        ("build_notes_updated", "TestFlight notes edited"),
    ],
    "google": [
        ("published", "Live (confirmed by Google)"), ("rollout_changed", "Rollout percentage changed"),
        ("rollout_paused", "Rollout paused"), ("rollout_resumed", "Rollout resumed"),
        ("rollout_completed", "Rollout reached 100%"), ("rollout_started", "First rollout percentage"),
        ("in_review", "In review"), ("approved", "Approved, not yet published"), ("rejected", "Rejected"),
        ("not_sent_for_review", "Not sent for review"), ("draft", "New release draft"),
        ("publication_unconfirmed", "Track set, publication not yet confirmed"),
        ("rollout_pending_confirmation", "Rollout set, confirmation pending"),
        ("notes_updated", "Release notes edited"), ("release_build_changed", "Different build selected"),
        ("release_renamed", "Release renamed"), ("status_changed", "Other status changes"),
        ("build_detected", "New Android build detected"), ("build_notes_updated", "Build notes edited"),
    ],
    "monitor": [
        ("monitor_connected", "Store connected"), ("monitor_attention", "Monitor needs attention"),
        ("monitor_recovered", "Monitor recovered"),
    ],
}


def load_env(path="setup.env"):
    values = {}
    if Path(path).is_file():
        for line in Path(path).read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("'\"")
    return {**values, **{key: os.environ[key] for key in
                         ("GCP_PROJECT", "GITHUB_REPO", "STATE_BUCKET", "MONITOR_SERVICE_ACCOUNT") if key in os.environ}}


def run(command, stdin=None):
    """Run a CLI without a shell; secrets only ever go through stdin."""
    try:
        done = subprocess.run(command, input=stdin, capture_output=True, text=True, timeout=90)
    except FileNotFoundError:
        return False, f"{command[0]} is not installed"
    except subprocess.TimeoutExpired:
        return False, f"{command[0]} timed out"
    return done.returncode == 0, (done.stdout if done.returncode == 0 else done.stderr.strip()[-500:])


def load_state(path, bucket):
    if path:
        return json.loads(Path(path).read_text()), path
    if bucket:
        ok, output = run(["gcloud", "storage", "cat", f"gs://{bucket}/state.json"])
        if ok:
            return json.loads(output), f"gs://{bucket}/state.json"
        print("Could not read the monitor state; only games named in config.json are listed:\n" + output, file=sys.stderr)
    return {}, None


def configured_ids(config):
    """App IDs config.json names for Discord, with the labels given to them."""
    found = {}
    for channel in discord_channels(config, include_disabled=True):
        for field in ("games", "exclude_apps"):
            value = channel.get(field)
            if isinstance(value, dict):
                found.update({str(key).lower(): str(name) for key, name in value.items()})
            elif isinstance(value, list):
                found.update({str(key).lower(): "" for key in value})
    return found


def games(state, config):
    """Each game once, keyed by bundle ID / package name so one entry covers both stores."""
    found = {}
    for key, scope in state.get("scopes", {}).items():
        store, app_id = scope.get("store") or key.split("/")[0], str(scope.get("app_id") or "")
        if store not in ("apple", "google") or not app_id or key.endswith("/builds"):
            continue
        bundle = str(scope.get("bundle_id") or (app_id if store == "google" else "")).lower()
        name = scope.get("app_name") or next((release.get("app_name") for release in scope.get("releases", {}).values()
                                              if release.get("app_name")), app_id)
        game = found.setdefault(bundle or app_id.lower(), {"key": bundle or app_id.lower(), "names": {}, "ids": set()})
        game["names"].setdefault(store, name)
        game["ids"] |= {app_id.lower(), bundle} - {""}
    known = set().union(*(game["ids"] for game in found.values())) if found else set()
    for app_id, label in configured_ids(config).items():
        if app_id not in known:  # Named in config.json but not (or no longer) seen in the stores.
            found[app_id] = {"key": app_id, "names": {"config": label or app_id}, "ids": {app_id}, "unseen": True}
    result = [{**game, "ids": sorted(game["ids"])} for game in found.values()]
    return sorted(result, key=lambda game: (next(iter(game["names"].values())).lower(), game["key"]))


def google_tracks(state, config):
    tracks = list(dict.fromkeys(["production", *config.get("google", {}).get("extra_tracks", [])]))
    for scope in state.get("scopes", {}).values():
        for release in scope.get("releases", {}).values():
            track = release.get("track")
            if scope.get("store") == "google" and track and release.get("kind") != "build" and track not in tracks:
                tracks.append(track)
    return tracks


def dumps(value, indent=0):
    """JSON in config.json's style: short lists and objects stay on one line."""
    flat = json.dumps(value, ensure_ascii=False)
    if not isinstance(value, (dict, list)) or len(flat) + indent <= 100 and not (
            isinstance(value, dict) and any(isinstance(item, (dict, list)) and item for item in value.values())):
        return flat
    pad, inner = " " * indent, " " * (indent + 2)
    if isinstance(value, list):
        items = [inner + dumps(item, indent + 2) for item in value]
        return "[\n" + ",\n".join(items) + "\n" + pad + "]"
    items = [f"{inner}{json.dumps(key, ensure_ascii=False)}: {dumps(item, indent + 2)}" for key, item in value.items()]
    return "{\n" + ",\n".join(items) + "\n" + pad + "}"


class Settings:
    def __init__(self, config_path, state, state_source, env):
        self.config_path, self.state, self.state_source, self.env = Path(config_path), state, state_source, env
        self.project, self.repo = env.get("GCP_PROJECT"), env.get("GITHUB_REPO")
        self.account = env.get("MONITOR_SERVICE_ACCOUNT") or (
            f"store-monitor@{self.project}.iam.gserviceaccount.com" if self.project else None)

    def config(self):
        return json.loads(self.config_path.read_text())

    def stored(self, names):
        """Which webhook secrets exist in Secret Manager (None when the project is unknown)."""
        if not self.project:
            return {name: None for name in names}
        check = lambda name: run(["gcloud", "secrets", "describe", name, "--project", self.project, "--format=value(name)"])[0]
        with ThreadPoolExecutor(8) as pool:
            return dict(zip(names, pool.map(check, names)))

    def data(self):
        config = self.config()
        names = sorted({channel.get("webhook_secret", "") for channel in config.get("discord", {}).get("channels", [])}
                       | {"DISCORD_WEBHOOK_URL"})
        return {"config": config, "games": games(self.state, config), "events": EVENTS,
                "tracks": google_tracks(self.state, config), "secrets": self.stored([n for n in names if n]),
                "project": self.project, "repo": self.repo, "state": self.state_source,
                "configPath": str(self.config_path.resolve())}

    def save(self, config):
        discord_channels(config, include_disabled=True)  # Raises on a bad id or secret name.
        if not isinstance(config.get("discord", {}).get("channels", []), list):
            raise ValueError("discord.channels must be a list")
        self.config_path.write_text(dumps(config) + "\n")
        return {"saved": str(self.config_path)}

    def store_webhook(self, name, url):
        if not WEBHOOK_SECRET.fullmatch(name):
            raise ValueError("Secret names must be DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL_<NAME>")
        if not discord.valid(url):
            raise ValueError("That is not a Discord webhook URL (https://discord.com/api/webhooks/…)")
        if not (self.project and self.repo):
            raise ValueError("Set GCP_PROJECT and GITHUB_REPO in setup.env to store webhooks from here")
        url, steps = url.strip(), []
        exists, _ = run(["gcloud", "secrets", "describe", name, "--project", self.project])
        command = (["gcloud", "secrets", "versions", "add", name] if exists else
                   ["gcloud", "secrets", "create", name, "--replication-policy=automatic"])
        steps.append(("Secret Manager", *run(command + ["--project", self.project, "--data-file=-"], url)))
        if steps[-1][1] and self.account:
            steps.append(("Monitor access", *run(["gcloud", "secrets", "add-iam-policy-binding", name, "--project",
                                                  self.project, f"--member=serviceAccount:{self.account}",
                                                  "--role=roles/secretmanager.secretAccessor", "--format=none"])))
        steps.append(("GitHub secret", *run(["gh", "secret", "set", name, "--repo", self.repo], url)))
        return {"steps": [{"step": step, "ok": ok, "detail": "" if ok else detail} for step, ok, detail in steps]}

    def test(self, name, url, channel_name):
        if not url:
            if not (self.project and WEBHOOK_SECRET.fullmatch(name or "")):
                raise ValueError("Paste the webhook URL, or store it first")
            ok, url = run(["gcloud", "secrets", "versions", "access", "latest", "--secret", name, "--project", self.project])
            if not ok:
                raise ValueError(f"Could not read {name} from Secret Manager")
        if not discord.valid(url):
            raise ValueError("That is not a Discord webhook URL")
        discord.Discord(url.strip()).post(discord.test_payload({"name": channel_name}))
        return {"sent": True}


def handler(settings, token, port):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def check(self):
            # The Host check stops DNS rebinding; the token header stops other sites posting here.
            if self.headers.get("Host") not in allowed_hosts:
                self.reply(403, {"error": "Forbidden host"})
                return False
            return True

        def do_GET(self):
            if not self.check():
                return
            if self.path == "/":
                body = PAGE.read_text().replace("__TOKEN__", token).encode()
                return self.send(200, body, "text/html; charset=utf-8")
            if self.path == "/api/data":
                return self.reply(200, settings.data())
            self.reply(404, {"error": "Not found"})

        def do_POST(self):
            if not self.check():
                return
            if self.headers.get("X-Settings-Token") != token:
                return self.reply(403, {"error": "Missing settings token; reload the page"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.path == "/api/config":
                    return self.reply(200, settings.save(body["config"]))
                if self.path == "/api/webhook":
                    return self.reply(200, settings.store_webhook(body.get("secret", ""), body.get("url", "")))
                if self.path == "/api/test":
                    return self.reply(200, settings.test(body.get("secret"), body.get("url", "").strip(), body.get("name")))
                self.reply(404, {"error": "Not found"})
            except Exception as error:  # Shown in the page; webhook URLs never appear in these messages.
                self.reply(400, {"error": str(error) or error.__class__.__name__})

        def reply(self, status, payload):
            self.send(status, json.dumps(payload).encode(), "application/json")

        def send(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local settings page for Discord channels")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--state", help="A local state.json instead of the one in STATE_BUCKET")
    parser.add_argument("--port", type=int, default=8740)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    env = load_env()
    bucket = env.get("STATE_BUCKET") or (f"{env['GCP_PROJECT']}-store-monitor" if env.get("GCP_PROJECT") else None)
    state, source = load_state(args.state, None if args.state else bucket)
    settings = Settings(args.config, state, source, env)
    token = secrets.token_urlsafe(24)
    for port in range(args.port, args.port + 20):  # The next free port if another app has this one.
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), handler(settings, token, port))
            break
        except OSError:
            continue
    else:
        raise RuntimeError(f"Ports {args.port}-{args.port + 19} are all in use; pass --port")
    url = f"http://127.0.0.1:{port}/"
    print(f"Settings page: {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
