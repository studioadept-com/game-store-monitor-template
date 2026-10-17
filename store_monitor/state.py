import base64
import json
import os
from pathlib import Path
from urllib.parse import quote

import google.auth
from google.auth.transport.requests import AuthorizedSession

from .events import empty_state
from .http import ApiError, Client
from .providers import required

STORAGE = "https://storage.googleapis.com"


def validate(state):
    if not isinstance(state, dict) or state.get("schema") != 1:
        raise RuntimeError("State schema invalid; refusing to reset notification history")
    for key, expected in (("scopes", dict), ("health", dict), ("outbox", list), ("initialized", list), ("sequence", int)):
        if not isinstance(state.get(key), expected):
            raise RuntimeError("State is malformed; refusing to reset notification history")
    return state


class LocalState:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        return validate(json.loads(self.path.read_text())) if self.path.exists() else empty_state()

    def save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(self.path)


class GitHubState:
    """Optimistic writes plus a workflow concurrency lock prevent lost checkpoints."""
    def __init__(self):
        self.client = Client("GitHub state")
        self.client.session.headers.update({"Authorization": "Bearer " + required("GITHUB_TOKEN"),
                                           "Accept": "application/vnd.github+json",
                                           "X-GitHub-Api-Version": "2022-11-28"})
        self.base = "https://api.github.com/repos/" + required("GITHUB_REPOSITORY")
        self.branch = os.getenv("STATE_BRANCH", "monitor-state")
        self.url = self.base + "/contents/state.json"
        self.sha = None
        self.branch_exists = False

    def load(self):
        try:
            self.client.request("GET", self.base + "/git/ref/heads/" + quote(self.branch, safe=""))
            self.branch_exists = True
        except ApiError as error:
            if error.status != 404:
                raise
            return empty_state()
        # If the branch exists but the file is missing/corrupt, fail closed.
        data = self.client.request("GET", self.url, params={"ref": self.branch})
        self.sha = data["sha"]
        if data.get("encoding") == "none":
            # Contents responses omit inline data above 1 MB; event history can
            # legitimately grow past that size. The Git blob endpoint retains it.
            data = self.client.request("GET", self.base + "/git/blobs/" + self.sha)
        return validate(json.loads(base64.b64decode(data["content"])))

    def save(self, state):
        content = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if not self.branch_exists:
            # Create an orphan state commit with its file atomically, so a stopped
            # first run cannot leave an empty state branch behind.
            blob = self.client.request("POST", self.base + "/git/blobs", json={"content": content, "encoding": "utf-8"})
            tree = self.client.request("POST", self.base + "/git/trees", json={"tree": [{"path": "state.json", "mode": "100644", "type": "blob", "sha": blob["sha"]}]})
            commit = self.client.request("POST", self.base + "/git/commits", json={"message": "Initialize monitor state", "tree": tree["sha"], "parents": []})
            self.client.request("POST", self.base + "/git/refs", json={"ref": "refs/heads/" + self.branch, "sha": commit["sha"]})
            self.branch_exists, self.sha = True, blob["sha"]
            return
        data = self.client.request("PUT", self.url, json={"message": "Checkpoint monitor state", "branch": self.branch,
                                  "sha": self.sha, "content": base64.b64encode(content.encode()).decode()})
        self.sha = data["content"]["sha"]


class GCSState:
    """One JSON object in Cloud Storage; generation preconditions reject concurrent writers."""
    def __init__(self, bucket, name="state.json"):
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/devstorage.read_write"])
        self.client = Client("Cloud Storage", AuthorizedSession(credentials))
        self.bucket, self.name = bucket, name
        self.url = STORAGE + "/storage/v1/b/" + quote(bucket, safe="") + "/o/" + quote(name, safe="")
        self.generation = 0  # 0 means "create only if absent".

    def load(self):
        try:
            self.generation = int(self.client.request("GET", self.url)["generation"])
        except ApiError as error:
            if error.status != 404:
                raise
            self.generation = 0
            return empty_state()
        data = self.client.request("GET", self.url, params={"alt": "media", "ifGenerationMatch": self.generation})
        return validate(data)

    def save(self, state):
        content = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        try:
            result = self.client.request(
                "POST", STORAGE + "/upload/storage/v1/b/" + quote(self.bucket, safe="") + "/o",
                params={"uploadType": "media", "name": self.name, "ifGenerationMatch": self.generation},
                data=content.encode(), headers={"Content-Type": "application/json"})
        except ApiError as error:
            if error.status == 412:
                raise RuntimeError("State was changed by another monitor run; this run's changes were not saved") from None
            raise
        self.generation = int(result["generation"])
