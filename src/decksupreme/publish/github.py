"""Task 6: commit the weekly ranking artifacts to GitHub through the REST API.

All files land in a single commit using the Git Data API (blobs, then a tree,
then a commit, then a ref update), so the repo never shows a half-updated week.
No git binary or extra dependency is needed inside the Airflow image.

* If GITHUB_TOKEN or GITHUB_REPO is unset, a warning is logged and the run is
  reported as skipped. The files are already on disk, so the pipeline does not fail.
* If the files already match the branch, no empty commit is made.
* If the branch moves between reading and updating it (another push), the
  whole sequence is retried on the new head.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from decksupreme.config import GitHubSettings, get_settings
from decksupreme.publish.artifacts import DEFAULT_RANKINGS_DIR, archive_name

log = logging.getLogger(__name__)

API = "https://api.github.com"
COMMIT_MESSAGE = "ci(data): update weekly card rankings [{week_id}]"


class GitHubPublishError(RuntimeError):
    pass


@dataclass
class PublishResult:
    status: str  # "committed" | "unchanged" | "skipped"
    week_id: str
    files: list[str] = field(default_factory=list)
    commit_sha: str | None = None
    commit_url: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [])}


def artifact_files(
    year: int, week: int, rankings_dir: Path | None = None
) -> list[tuple[str, Path]]:
    """(repo path, local path) pairs for one week's artifacts."""
    d = rankings_dir or DEFAULT_RANKINGS_DIR
    names = ["latest.json", archive_name(year, week), "README.md"]
    return [(f"rankings/{n}", d / n) for n in names]


class GitHubClient:
    def __init__(self, token: str, repo: str, session: requests.Session | None = None):
        self.repo = repo
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "decksupreme-publisher",
            }
        )

    def _call(self, method: str, path: str, **kw: Any) -> Any:
        url = f"{API}/repos/{self.repo}{path}"
        resp = self.session.request(method, url, timeout=30, **kw)
        if resp.status_code >= 400:
            hint = {
                401: "token is invalid or expired",
                403: "token lacks Contents: read & write on this repo",
                404: "repo or branch not found (or token can't see it)",
            }.get(resp.status_code, "")
            raise GitHubPublishError(
                f"{method} {path} -> HTTP {resp.status_code} {hint}: {resp.text[:200]}".strip(),
            )
        return resp.json() if resp.content else None

    def head(self, branch: str) -> tuple[str, str]:
        """(commit sha, tree sha) at the tip of branch."""
        ref = self._call("GET", f"/git/ref/heads/{branch}")
        commit_sha = ref["object"]["sha"]
        commit = self._call("GET", f"/git/commits/{commit_sha}")
        return commit_sha, commit["tree"]["sha"]

    def commit_files(
        self, branch: str, files: Sequence[tuple[str, bytes]], message: str
    ) -> tuple[str | None, str | None]:
        """One commit with all files. Returns (sha, html_url), or (None, None) if unchanged."""
        parent, base_tree = self.head(branch)
        tree = []
        for path, content in files:
            blob = self._call(
                "POST", "/git/blobs",
                json={"content": base64.b64encode(content).decode(), "encoding": "base64"},
            )
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        new_tree = self._call("POST", "/git/trees", json={"base_tree": base_tree, "tree": tree})
        if new_tree["sha"] == base_tree:
            return None, None
        commit = self._call(
            "POST", "/git/commits",
            json={"message": message, "tree": new_tree["sha"], "parents": [parent]},
        )
        self._call(
            "PATCH", f"/git/refs/heads/{branch}", json={"sha": commit["sha"], "force": False}
        )
        return commit["sha"], commit.get("html_url")


def publish_rankings(
    year: int,
    week: int,
    *,
    settings: GitHubSettings | None = None,
    rankings_dir: Path | None = None,
    client: GitHubClient | None = None,
    attempts: int = 3,
) -> PublishResult:
    week_id = f"{year:04d}-{week:02d}"
    gh = settings or get_settings().github
    pairs = artifact_files(year, week, rankings_dir)
    repo_paths = [p for p, _ in pairs]

    missing = [str(local) for _, local in pairs if not local.exists()]
    if missing:
        raise GitHubPublishError(f"artifacts missing (run scoring first): {', '.join(missing)}")

    if not gh.token or not gh.repo:
        unset = [n for n, v in (("GITHUB_TOKEN", gh.token), ("GITHUB_REPO", gh.repo)) if not v]
        reason = f"{' and '.join(unset)} not set; artifacts kept locally only"
        log.warning("GitHub publish skipped: %s", reason)
        return PublishResult("skipped", week_id, repo_paths, reason=reason)

    client = client or GitHubClient(gh.token, gh.repo)
    files = [(path, local.read_bytes()) for path, local in pairs]
    message = COMMIT_MESSAGE.format(week_id=week_id)

    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            sha, url = client.commit_files(gh.branch, files, message)
            break
        except GitHubPublishError as exc:
            # 422 on the ref update = branch moved (non-fast-forward); rebuild on the new head.
            if "HTTP 422" not in str(exc) or attempt == attempts:
                raise
            log.warning("branch %s moved during publish; retrying (%d/%d)", gh.branch, attempt,
                        attempts)
            last = exc
    else:  # pragma: no cover - loop always breaks or raises
        raise GitHubPublishError(str(last))

    if sha is None:
        log.info("GitHub %s@%s already has these artifacts; nothing to commit", gh.repo, gh.branch)
        return PublishResult("unchanged", week_id, repo_paths)
    log.info("committed %s to %s@%s", sha[:7], gh.repo, gh.branch)
    return PublishResult("committed", week_id, repo_paths, commit_sha=sha, commit_url=url)
