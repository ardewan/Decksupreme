"""Task 6 publisher against an in-memory fake of the GitHub Git Data API."""

from __future__ import annotations

import base64
import hashlib
import json
import logging

import pytest

from decksupreme.config import GitHubSettings
from decksupreme.publish.github import (
    GitHubClient,
    GitHubPublishError,
    artifact_files,
    publish_rankings,
)

REPO = "owner/decksupreme"


class Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body
        self.content = b"" if body is None else json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


def _sha(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True).encode()).hexdigest()


class FakeGitHub:
    """Enough of the Git Data API to commit files and detect no-op trees."""

    def __init__(self, files=None, status_override=None, race_once=False):
        self.headers = {}
        self.trees = {}
        self.commits = {}
        self.blobs = {}
        root = self._make_tree(files or {"README.md": b"hi"})
        self.head = self._make_commit(root, [], "init")
        self.status_override = status_override or {}
        self.race_once = race_once
        self.calls = []

    def _make_tree(self, files):
        sha = _sha({k: base64.b64encode(v).decode() for k, v in files.items()})
        self.trees[sha] = dict(files)
        return sha

    def _make_commit(self, tree, parents, message):
        sha = _sha({"tree": tree, "parents": parents, "message": message})
        self.commits[sha] = {"tree": tree, "parents": parents, "message": message}
        return sha

    def files_at_head(self):
        return self.trees[self.commits[self.head]["tree"]]

    def request(self, method, url, timeout=None, json=None):
        path = url.split(f"/repos/{REPO}", 1)[1]
        self.calls.append((method, path))
        for (m, prefix), status in self.status_override.items():
            if m == method and path.startswith(prefix):
                return Resp(status, {"message": "nope"})
        if method == "GET" and path == "/git/ref/heads/main":
            return Resp(200, {"object": {"sha": self.head}})
        if method == "GET" and path.startswith("/git/ref/heads/"):
            return Resp(404, {"message": "Not Found"})
        if method == "GET" and path.startswith("/git/commits/"):
            return Resp(200, {"tree": {"sha": self.commits[path.rsplit("/", 1)[1]]["tree"]}})
        if method == "POST" and path == "/git/blobs":
            data = base64.b64decode(json["content"])
            sha = hashlib.sha1(data).hexdigest()
            self.blobs[sha] = data
            return Resp(201, {"sha": sha})
        if method == "POST" and path == "/git/trees":
            files = dict(self.trees[json["base_tree"]])
            for e in json["tree"]:
                files[e["path"]] = self.blobs[e["sha"]]
            return Resp(201, {"sha": self._make_tree(files)})
        if method == "POST" and path == "/git/commits":
            sha = self._make_commit(json["tree"], json["parents"], json["message"])
            return Resp(201, {"sha": sha, "html_url": f"https://github.com/{REPO}/commit/{sha}"})
        if method == "PATCH" and path == "/git/refs/heads/main":
            parent = self.commits[json["sha"]]["parents"][0]
            if self.race_once:
                self.race_once = False
                self.head = self._make_commit(self.commits[self.head]["tree"], [self.head], "x")
            if parent != self.head:
                return Resp(422, {"message": "Update is not a fast forward"})
            self.head = json["sha"]
            return Resp(200, {"object": {"sha": json["sha"]}})
        raise AssertionError(f"unexpected {method} {path}")


SETTINGS = GitHubSettings(repo=REPO, branch="main", token="t0k")


@pytest.fixture
def rankings(tmp_path):
    for repo_path, local in artifact_files(2026, 45, tmp_path):
        local.write_text(f"content of {repo_path}\n")
    return tmp_path


def _client(fake):
    return GitHubClient(SETTINGS.token, REPO, session=fake)


def test_artifact_paths():
    assert [p for p, _ in artifact_files(2026, 5)] == [
        "rankings/latest.json", "rankings/rankings_2026_05.json", "rankings/README.md",
    ]


def test_auth_headers():
    fake = FakeGitHub()
    _client(fake)
    assert fake.headers["Authorization"] == "Bearer t0k"
    assert fake.headers["Accept"] == "application/vnd.github+json"


def test_commits_all_files_in_one_commit(rankings):
    fake = FakeGitHub()
    before = fake.head
    result = publish_rankings(2026, 45, settings=SETTINGS, rankings_dir=rankings,
                              client=_client(fake))
    assert result.status == "committed"
    assert result.commit_sha == fake.head
    commit = fake.commits[fake.head]
    assert commit["parents"] == [before]
    assert commit["message"] == "ci(data): update weekly card rankings [2026-45]"
    files = fake.files_at_head()
    assert files["rankings/latest.json"] == b"content of rankings/latest.json\n"
    assert "rankings/rankings_2026_45.json" in files and "rankings/README.md" in files
    assert files["README.md"] == b"hi"  # untouched files preserved via base_tree
    assert sum(1 for m, p in fake.calls if m == "POST" and p == "/git/commits") == 1


def test_rerun_with_same_content_makes_no_commit(rankings):
    fake = FakeGitHub()
    publish_rankings(2026, 45, settings=SETTINGS, rankings_dir=rankings, client=_client(fake))
    head = fake.head
    again = publish_rankings(2026, 45, settings=SETTINGS, rankings_dir=rankings,
                             client=_client(fake))
    assert again.status == "unchanged"
    assert fake.head == head


def test_retries_when_branch_moves(rankings):
    fake = FakeGitHub(race_once=True)
    result = publish_rankings(2026, 45, settings=SETTINGS, rankings_dir=rankings,
                              client=_client(fake))
    assert result.status == "committed"
    assert fake.commits[fake.head]["message"].startswith("ci(data)")
    assert sum(1 for m, _ in fake.calls if m == "PATCH") == 2


@pytest.mark.parametrize(
    ("token", "repo", "names"),
    [
        ("", REPO, "GITHUB_TOKEN"),
        ("t", "", "GITHUB_REPO"),
        ("", "", "GITHUB_TOKEN and GITHUB_REPO"),
    ],
)
def test_skips_with_warning_when_unconfigured(rankings, caplog, token, repo, names):
    s = GitHubSettings(repo=repo, branch="main", token=token)
    with caplog.at_level(logging.WARNING):
        result = publish_rankings(2026, 45, settings=s, rankings_dir=rankings)
    assert result.status == "skipped"
    assert names in result.reason
    assert "GitHub publish skipped" in caplog.text
    assert all(local.exists() for _, local in artifact_files(2026, 45, rankings))


def test_missing_artifacts_fail_even_when_unconfigured(tmp_path):
    with pytest.raises(GitHubPublishError, match="run scoring first"):
        publish_rankings(2026, 45, settings=GitHubSettings("", "main", ""), rankings_dir=tmp_path)


@pytest.mark.parametrize(("status", "hint"), [(401, "invalid or expired"), (403, "read & write")])
def test_auth_errors_are_explained(rankings, status, hint):
    fake = FakeGitHub(status_override={("GET", "/git/ref"): status})
    with pytest.raises(GitHubPublishError, match=hint):
        publish_rankings(2026, 45, settings=SETTINGS, rankings_dir=rankings, client=_client(fake))


def test_unknown_branch(rankings):
    s = GitHubSettings(repo=REPO, branch="nope", token="t")
    with pytest.raises(GitHubPublishError, match="404"):
        publish_rankings(2026, 45, settings=s, rankings_dir=rankings,
                         client=GitHubClient("t", REPO, session=FakeGitHub()))
