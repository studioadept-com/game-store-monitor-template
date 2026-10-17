import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlparse

import google.auth
from google.auth.transport.requests import AuthorizedSession
import jwt

from .events import APPLE_STATES, GOOGLE_STATES
from .http import ApiError, Client
from .polling import last_attempt, lifecycle_order, track_fingerprint

APPLE = "https://api.appstoreconnect.apple.com"
PLAY = "https://androidpublisher.googleapis.com/androidpublisher/v3"
REPORTING = "https://playdeveloperreporting.googleapis.com/v1beta1/apps:search"
SCOPES = ["https://www.googleapis.com/auth/androidpublisher",
          "https://www.googleapis.com/auth/playdeveloperreporting"]


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing {name}")
    return value


def apple_rollout(attributes):
    phase = attributes.get("phasedReleaseState")
    if phase == "COMPLETE":
        return 100
    day = attributes.get("currentDayNumber")
    if phase in ("ACTIVE", "PAUSED") and isinstance(day, int) and 1 <= day <= 7:
        return [1, 2, 5, 10, 20, 50, 100][day - 1]
    return None


def choose_notes(localizations):
    """Choose one deterministic localization; never invent a feature summary."""
    notes = {item.get("locale") or item.get("language", ""): (item.get("whatsNew") or item.get("text") or "").strip()
             for item in localizations}
    notes = {locale: text for locale, text in notes.items() if text}
    for locale in ("en-US", "en-GB", "en", *sorted(notes)):
        if locale in notes:
            return notes[locale], locale
    return "", ""


def related_items(item, relationship, included):
    data = item.get("relationships", {}).get(relationship, {}).get("data")
    references = data if isinstance(data, list) else [data] if data else []
    return [included[(ref["type"], ref["id"])] for ref in references if (ref["type"], ref["id"]) in included]


def google_release(app_id, app_name, track, release, lifecycle=None):
    codes = sorted({int(code) for code in release.get("versionCodes", [])})
    raw_name = release.get("name", "")
    # Play's API exposes a release name, not a guaranteed Android versionName.
    match = re.search(r"(?<![\d.])(\d+\.\d+(?:\.\d+)*(?:[-+][\w.]+)?)(?![\d.])", raw_name)
    version = match.group(1) if match else raw_name or ("code " + ", ".join(map(str, codes)) if codes else "unnamed")
    phase = release.get("status")
    state = GOOGLE_STATES.get(lifecycle, lifecycle.lower().replace("_", " ") if lifecycle else None)
    if not state:
        state = "draft" if phase == "draft" else "release status unknown"
    # edits.tracks describes intended deployment too. Do not claim production until
    # the independent lifecycle endpoint explicitly says PUBLISHED.
    rollout = None
    if state == "published":
        if phase == "completed":
            rollout = 100
        elif release.get("userFraction") is not None:
            rollout = round(float(release["userFraction"]) * 100, 5)
    configured_rollout = (100 if phase == "completed" else
                          round(float(release["userFraction"]) * 100, 5)
                          if phase in ("inProgress", "halted") and release.get("userFraction") is not None else None)
    notes, locale = choose_notes(release.get("releaseNotes", []))
    return {
        "store": "google", "app_id": app_id, "app_name": app_name,
        "version": version, "track": track, "status": state,
        "phase": phase if state == "published" else None, "rollout": rollout,
        "track_phase": phase, "configured_rollout": configured_rollout,
        "codes": codes,
        "build_number": ", ".join(map(str, codes)),
        "notes": notes, "notes_locale": locale, "notes_source": "Google Play release notes",
    }


def identity(codes, name):
    return str(max(int(code) for code in codes)) if codes else "draft:" + name


def cached_track_records(app_id, app_name, track, releases, previous):
    """Refresh rollout data without inventing a new publication/review decision."""
    statuses = {value: key for key, value in GOOGLE_STATES.items()}
    records = {}
    for item in releases:
        key = identity(item.get("versionCodes", []), item.get("name", ""))
        prior = previous.get(key, {})
        lifecycle = statuses.get(prior.get("status"))
        if item.get("status") == "draft":
            lifecycle = "DRAFT"
        elif lifecycle == "DRAFT":
            # A cached draft decision must not hide a newly configured rollout.
            lifecycle = None
        record = google_release(app_id, app_name, track, item, lifecycle)
        if record["status"] == "release status unknown":
            record["status"] = "track configured; publication not yet confirmed"
        records[key] = record
    return records


class AppleProvider:
    def __init__(self):
        now = int(time.time())
        token = jwt.encode({"iss": required("ASC_ISSUER_ID"), "iat": now - 30,
                            "exp": now + 1100, "aud": "appstoreconnect-v1"},
                           required("ASC_PRIVATE_KEY").replace("\\n", "\n"), algorithm="ES256",
                           headers={"kid": required("ASC_KEY_ID"), "typ": "JWT"})
        self.client = Client("App Store Connect")
        self.headers = {"Authorization": "Bearer " + token}

    def pages(self, url, params=None):
        seen = set()
        while url:
            if urlparse(url).netloc != "api.appstoreconnect.apple.com" or urlparse(url).scheme != "https" or url in seen:
                raise RuntimeError("Invalid App Store pagination")
            seen.add(url)
            page = self.client.request("GET", url, params=params, headers=self.headers)
            if not isinstance(page.get("data"), list):
                raise RuntimeError("Invalid App Store response")
            yield page
            url, params = page.get("links", {}).get("next"), None

    def collect_builds(self, app_id, app_name, bundle_id=None):
        records = {}
        params = {"filter[app]": app_id, "limit": 200, "sort": "-uploadedDate",
                  "include": "preReleaseVersion,betaBuildLocalizations"}
        for page in self.pages(APPLE + "/v1/builds", params):
            included = {(item["type"], item["id"]): item for item in page.get("included", [])}
            for item in page["data"]:
                attrs = item["attributes"]
                versions = related_items(item, "preReleaseVersion", included)
                prerelease = versions[0]["attributes"] if versions else {}
                localizations = related_items(item, "betaBuildLocalizations", included)
                notes, locale = choose_notes([value["attributes"] for value in localizations])
                records[item["id"]] = {
                    "kind": "build", "store": "apple", "app_id": app_id, "app_name": app_name, "bundle_id": bundle_id,
                    "version": prerelease.get("version", "unknown"), "build_number": attrs["version"],
                    "track": prerelease.get("platform", "IOS"), "status": attrs.get("processingState", "UNKNOWN"),
                    "uploaded_at": attrs.get("uploadedDate"),
                    "notes": notes, "notes_locale": locale, "notes_source": "TestFlight What to Test",
                }
        return {"kind": "builds", "store": "apple", "app_id": app_id, "app_name": app_name, "bundle_id": bundle_id,
                "releases": records}

    def collect(self, config, previous, snapshots, errors, successes):
        apps = {}
        for page in self.pages(APPLE + "/v1/apps", {"limit": 200}):
            for app in page["data"]:
                # The bundle ID lets config.json name a game the same way on both stores.
                apps[app["id"]] = (app["attributes"]["name"], app["attributes"].get("bundleId"))
        if not apps:
            raise RuntimeError("No App Store apps visible; check API key access")
        for scope, value in previous.items():
            if value["store"] == "apple":
                apps.setdefault(value["app_id"], (value["app_name"], value.get("bundle_id")))
        successes.add("apple/discovery")
        # Read-only App Store calls are independent per app; a few in parallel keep
        # the run short while staying far below Apple's hourly request limit.
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda app: self.collect_app(config, app[0], *app[1], snapshots, errors, successes), apps.items()))

    def collect_app(self, config, app_id, app_name, bundle_id, snapshots, errors, successes):
        scope = "apple/" + app_id
        try:
            records = {}
            includes = "appStoreVersionPhasedRelease"
            if config.get("release_notes", False):
                includes += ",build,appStoreVersionLocalizations"
            params = {"limit": 200, "include": includes}
            for page in self.pages(APPLE + f"/v1/apps/{quote(app_id, safe='')}/appStoreVersions", params):
                included = {(item["type"], item["id"]): item for item in page.get("included", [])}
                phases = {item["id"]: item["attributes"] for item in page.get("included", []) if item["type"] == "appStoreVersionPhasedReleases"}
                for item in page["data"]:
                    attrs = item["attributes"]
                    raw = attrs.get("appVersionState") or attrs.get("appStoreState") or "UNKNOWN"
                    relationship = item.get("relationships", {}).get("appStoreVersionPhasedRelease", {})
                    related = relationship.get("data")
                    phase = phases.get(related["id"]) if related else None
                    if related and phase is None:
                        raise RuntimeError("App Store phased release missing from response")
                    builds = related_items(item, "build", included)
                    localizations = related_items(item, "appStoreVersionLocalizations", included)
                    notes, locale = choose_notes([value["attributes"] for value in localizations])
                    records[item["id"]] = {
                        "store": "apple", "app_id": app_id, "app_name": app_name, "bundle_id": bundle_id,
                        "version": attrs["versionString"], "track": attrs.get("platform", "IOS"),
                        "status": APPLE_STATES.get(raw, raw.lower().replace("_", " ")),
                        "phase": (phase or {}).get("phasedReleaseState"),
                        "rollout": apple_rollout(phase) if phase else None,
                        "build_number": builds[0]["attributes"].get("version") if builds else None,
                        "notes": notes, "notes_locale": locale, "notes_source": "App Store release notes",
                    }
            snapshots[scope] = {"store": "apple", "app_id": app_id, "app_name": app_name, "bundle_id": bundle_id,
                                "releases": records}
            successes.add(scope)
        except (ApiError, RuntimeError, KeyError, ValueError) as error:
            errors[scope] = safe_error(error)
        if config.get("monitor_builds", False):
            build_scope = scope + "/builds"
            try:
                snapshots[build_scope] = self.collect_builds(app_id, app_name, bundle_id)
                successes.add(build_scope)
            except (ApiError, RuntimeError, KeyError, ValueError) as error:
                errors[build_scope] = safe_error(error)


def safe_error(error):
    # KeyError and auth-library exception strings can contain private server data.
    return str(error) if isinstance(error, ApiError) or type(error) is RuntimeError else type(error).__name__


class GoogleProvider:
    def __init__(self):
        credentials, _ = google.auth.default(scopes=SCOPES)
        self.client = Client("Google Play", AuthorizedSession(credentials))

    def collect_artifacts(self, edit_url, app_id, app_name, tracks, previous=None):
        metadata = {}
        for track in tracks:
            for release in track.get("releases", []):
                for code in release.get("versionCodes", []):
                    metadata[str(code)] = google_release(app_id, app_name, track["track"], release)
        records = {}
        for resource in ("bundles", "apks"):
            for artifact in self.client.request("GET", edit_url + "/" + resource).get(resource, []):
                code = str(artifact["versionCode"])
                # Artifacts remain after their release leaves the track. Missing
                # track metadata is not evidence that somebody deleted build notes.
                info = metadata.get(code, (previous or {}).get(code, {}))
                records[code] = {
                    "kind": "build", "store": "google", "app_id": app_id, "app_name": app_name,
                    "version": info.get("version", "unknown"), "build_number": code,
                    "track": "Android", "status": "DETECTED", "artifact_kind": resource,
                    "notes": info.get("notes", ""), "notes_locale": info.get("notes_locale", ""),
                    "notes_source": "Google Play release notes",
                }
        return {"kind": "builds", "store": "google", "app_id": app_id, "app_name": app_name, "releases": records}

    def collect(self, config, previous, snapshots, errors, successes):
        retry_after = max((value.get("lifecycle_retry_after", 0) for value in previous.values() if value["store"] == "google"), default=0)
        if retry_after and any((value.get("lifecycle_checked_at") or 0) >= retry_after
                               for value in previous.values() if value["store"] == "google"):
            retry_after = 0  # A recovered scope supersedes stale cooldown metadata on an inaccessible app.
        targets = {}
        apps = dict(config.get("apps", {}))
        if config.get("discover_apps", True):
            params = {"pageSize": 1000}
            seen = set()
            try:
                while True:
                    page = self.client.request("GET", REPORTING, params=params)
                    for app in page.get("apps", []):
                        apps.setdefault(app["packageName"], app.get("displayName") or app["packageName"])
                    token = page.get("nextPageToken")
                    if not token:
                        break
                    if token in seen:
                        raise RuntimeError("Repeated Google pagination token")
                    seen.add(token)
                    params["pageToken"] = token
                if not apps:
                    raise RuntimeError("No Google Play apps visible; grant account-wide Play Console access")
                successes.add("google/discovery")
            except (ApiError, RuntimeError, KeyError, ValueError) as error:
                errors["google/discovery"] = safe_error(error)
        for value in previous.values():
            if value["store"] == "google":
                apps.setdefault(value["app_id"], value["app_name"])
        if not apps:
            raise RuntimeError(errors.get("google/discovery", "No Google Play apps configured"))
        if not config.get("discover_apps", True):
            successes.add("google/discovery")
        for app_id, app_name in apps.items():
            scope = "google/" + app_id
            base = PLAY + "/applications/" + quote(app_id, safe="")
            edit_id = None
            try:
                # A dedicated monitoring service account is essential: inserting
                # an edit invalidates that SAME account's previous open edit.
                edit_id = self.client.request("POST", base + "/edits", json={})["id"]
                edit_url = base + "/edits/" + quote(edit_id, safe="")
                tracks = self.client.request("GET", edit_url + "/tracks").get("tracks", [])
                if config.get("monitor_builds", False):
                    try:
                        snapshots[scope + "/builds"] = self.collect_artifacts(edit_url, app_id, app_name, tracks, previous.get(scope + "/builds", {}).get("releases", {}))
                        successes.add(scope + "/builds")
                    except (ApiError, RuntimeError, KeyError, ValueError) as error:
                        errors[scope + "/builds"] = safe_error(error)
                known = {track["track"]: track for track in tracks}
                for track in config.get("extra_tracks", []):
                    known.setdefault(track, {"track": track, "releases": []})
                for value in previous.values():
                    if value.get("app_id") == app_id and value.get("store") == "google" and value.get("kind") != "builds":
                        for record in value["releases"].values():
                            known.setdefault(record["track"], {"track": record["track"], "releases": []})
                for track_name, track in known.items():
                    targets[scope + "/" + track_name] = {"app_id": app_id, "app_name": app_name,
                                                        "track": track, "track_name": track_name, "base": base}
                successes.add(scope)
            except (ApiError, RuntimeError, KeyError, ValueError) as error:
                errors[scope] = safe_error(error)
            finally:
                if edit_id:
                    try:
                        self.client.request("DELETE", base + "/edits/" + quote(edit_id, safe=""), safe_retry=True)
                        successes.add(scope + "/cleanup")
                    except ApiError as error:
                        if error.status not in (404, 409):
                            errors[scope + "/cleanup"] = safe_error(error)
        # Collect all tracks before scheduling, so app iteration order cannot starve
        # later apps. Temporary edits are already discarded before these GET calls.
        now = int(time.time())
        selected = lifecycle_order(targets, previous, config, now) if retry_after <= now else []
        queried = 0
        quota_blocked = bool(retry_after)
        for track_scope in selected + [scope for scope in targets if scope not in selected]:
            target = targets[track_scope]
            app_id, app_name = target["app_id"], target["app_name"]
            track_name, track = target["track_name"], target["track"]
            prior = previous.get(track_scope, {})
            metadata = {"store": "google", "app_id": app_id, "app_name": app_name,
                        "track_baselined": True, "lifecycle_checked_at": prior.get("lifecycle_checked_at"),
                        "baseline_pending": prior.get("baseline_pending", False),
                        "lifecycle_attempted_at": last_attempt(prior),
                        "lifecycle_fingerprint": prior.get("lifecycle_fingerprint")}
            try:
                summaries = None
                if track_scope in selected and retry_after <= now:
                    if queried:
                        time.sleep(max(0, float(config.get("lifecycle_request_spacing_seconds", 2))))
                    metadata["lifecycle_attempted_at"] = int(time.time())
                    queried += 1
                    try:
                        # Disable automatic GET retries: each attempt consumes budget,
                        # and quota failures must stop further requests immediately.
                        summaries = self.client.request("GET", target["base"] + "/tracks/" + quote(track_name, safe="") + "/releases", safe_retry=False).get("releases", [])
                        retry_after, quota_blocked = 0, False
                    except ApiError as error:
                        if ("Listing releases quota exceeded" in str(error) or error.status == 429
                                or error.reason in ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded")):
                            retry_after = int(time.time()) + max(300, int(config.get("lifecycle_quota_cooldown_seconds", 3600)))
                            quota_blocked = True
                        elif error.status == 404 and not track.get("releases"):
                            summaries = []  # Cache absent optional tracks and their request cost.
                        else:
                            raise
                if summaries is None:
                    records = cached_track_records(app_id, app_name, track_name, track.get("releases", []), prior.get("releases", {}))
                    snapshots[track_scope] = {**metadata, "releases": records,
                                              "lifecycle_degraded": True}
                    # A deferred lifecycle check cannot recover a failed lifecycle GET.
                    continue
                track_releases = {}
                for release in track.get("releases", []):
                    key = identity(release.get("versionCodes", []), release.get("name", ""))
                    if key in track_releases:
                        raise RuntimeError("Ambiguous Play release identity")
                    track_releases[key] = release
                merged = {}
                for summary in summaries:
                    codes = [item["versionCode"] for item in summary.get("activeArtifacts", [])]
                    name = summary.get("releaseName", "")
                    key = identity(codes, name)
                    existing = track_releases.get(key, {"name": name, "versionCodes": codes})
                    lifecycle = summary.get("releaseLifecycleState", "").removeprefix("RELEASE_LIFECYCLE_STATE_")
                    if key in merged:
                        raise RuntimeError("Ambiguous Play lifecycle identity")
                    merged[key] = (existing, lifecycle)
                records = {key: google_release(app_id, app_name, track_name, release, lifecycle)
                           for key, (release, lifecycle) in merged.items()}
                snapshots[track_scope] = {**metadata, "releases": records, "lifecycle_degraded": False,
                                          "baseline_pending": False,
                                          "lifecycle_checked_at": int(time.time()),
                                          "lifecycle_fingerprint": track_fingerprint(track)}
                successes.add(track_scope)
            except (ApiError, RuntimeError, KeyError, ValueError) as error:
                # Persist failed attempts too, so restarts cannot refill the budget.
                snapshots[track_scope] = {**metadata, "releases": prior.get("releases", {}),
                                          "baseline_pending": prior.get("baseline_pending", track_scope not in previous),
                                          "lifecycle_degraded": True}
                errors[track_scope] = safe_error(error)
        print(f"Google release-status requests: {queried}; limit {config.get('lifecycle_max_requests', 6)} per {config.get('lifecycle_window_seconds', 120)} seconds; {len(targets)} tracks available.")
        for snapshot in snapshots.values():
            if snapshot["store"] == "google":
                snapshot["lifecycle_retry_after"] = retry_after
        if quota_blocked:
            errors["google/lifecycle_api"] = "Google release-status quota exceeded; rollout checks continue using the track API, but review/publication decisions may be delayed. Lifecycle requests back off for one hour"
        elif queried:
            successes.add("google/lifecycle_api")
