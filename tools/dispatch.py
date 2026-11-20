#!/usr/bin/env python3
"""為新的 Ready PR head 凍結合併提交並派一次 Windows 驗證。

由本倉庫的排程觸發。被驗證的倉庫是私有的，而且不持有任何指向這裡的憑證：
hoshi-ci 主動去拉，沒有人往這裡推。

對 `targets.json` 的 `windows` 清單裡每個倉庫，以 HOSHIVEL_CI_TOKEN（唯讀）列出
同倉 Ready PR 的 head、base 與合併提交，丟掉已經有 `windows.yml` run 的 head（不論結果），其餘每個 head
以本倉庫自己的 GITHUB_TOKEN 派一個 run。run 的標題是 `Windows <倉庫>@<sha>`，
這個標題記錄已派出的 head。base 單獨前進不另派工；工作驗證已凍結的合併樹。

分支名只讀不印：日誌只寫倉庫與 commit。

用法：
  dispatch.py                         派工（需要 SOURCE_TOKEN、GH_TOKEN、GITHUB_REPOSITORY）
  dispatch.py check --repo R --sha H --base B --merge M --pr N  檢查工作身分
  dispatch.py selftest                只跑內建案例

只用標準庫。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ROOT / "targets.json"
API = "https://api.github.com"
WORKFLOW = "windows.yml"
DEFAULT_BRANCH = "main"

REPO = re.compile(r"^[A-Za-z0-9._-]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
TITLE = re.compile(r"^Windows (?P<repo>[A-Za-z0-9._-]+)@(?P<sha>[0-9a-f]{40})$")

# At most this many runs per tick; the rest wait for the next one.
MAX_DISPATCH = 2
# Read complete history within this bound. Refuse an incomplete scan rather
# than quietly redispatch a revision whose old result has fallen off the page.
RUN_PAGES = 100


@dataclass(frozen=True)
class Candidate:
    repository: str
    number: int
    head: str
    base: str
    merge: str

    def inputs(self) -> dict[str, str]:
        return {"repo": self.repository, "pr": str(self.number), "sha": self.head,
                "base": self.base, "merge": self.merge}


class SourceReadError(Exception):
    def __init__(self, status: int):
        self.status = status


def title(repo: str, sha: str) -> str:
    return f"Windows {repo}@{sha}"


def load_targets(path: Path = TARGETS, *, enabled: bool = False) -> list[str]:
    document = json.loads(path.read_text(encoding="utf-8"))
    repos = document.get("windows")
    if not isinstance(repos, list) or not repos or not all(isinstance(r, str) and REPO.match(r) for r in repos):
        raise ValueError(f"{path.name} 的 windows 必須是非空的倉庫名清單")
    if len(set(repos)) != len(repos):
        raise ValueError(f"{path.name} 的 windows 有重複")
    if not enabled:
        return repos
    active = document.get("enabled_windows", [])
    if not isinstance(active, list) or any(r not in repos for r in active) or len(set(active)) != len(active):
        raise ValueError("invalid enabled Windows list")
    return active


def check(repo: str, sha: str, base: str, merge: str, pr: str,
          path: Path = TARGETS) -> str | None:
    """Return the reason the inputs are rejected, or None."""
    if repo not in load_targets(path):
        return "repository is not in the Windows allowlist"
    if not all(isinstance(value, str) and SHA.fullmatch(value) for value in (sha, base, merge)):
        return "head, base and merge must be 40 lowercase hexadecimal characters"
    if not isinstance(pr, str) or not pr.isascii() or not pr.isdigit() or int(pr) < 1:
        return "PR number must be positive"
    return None


def select(candidates: list[Candidate], seen: set[tuple[str, str]],
           limit: int) -> tuple[list[Candidate], int]:
    """Select one immutable snapshot for each previously undispatched head."""
    pending = []
    selected = set(seen)
    for candidate in sorted(candidates, key=lambda item: (item.repository, item.number, item.head)):
        key = (candidate.repository, candidate.head)
        if key not in selected:
            pending.append(candidate)
            selected.add(key)
    return pending[:limit], max(0, len(pending) - limit)


def api(method: str, path: str, token: str, body: dict | None = None):
    request = urllib.request.Request(
        API + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "hoshi-ci-dispatch",
            **({} if body is None else {"Content-Type": "application/json"}),
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def source_api(path: str, token: str):
    try:
        return api("GET", path, token)
    except urllib.error.HTTPError as error:
        raise SourceReadError(error.code) from None


def eligible(owner: str, repo: str, pr: dict) -> bool:
    if not isinstance(pr, dict) or type(pr.get("number")) is not int or pr["number"] < 1:
        return False
    source = f"{owner}/{repo}"
    head, base = pr.get("head") or {}, pr.get("base") or {}
    if not isinstance(head, dict) or not isinstance(base, dict):
        return False
    if not isinstance(head.get("repo"), dict) or not isinstance(base.get("repo"), dict):
        return False
    return (pr.get("state") == "open" and pr.get("draft") is False
            and (head.get("repo") or {}).get("full_name") == source
            and (base.get("repo") or {}).get("full_name") == source
            and base.get("ref") == DEFAULT_BRANCH
            and all(isinstance(v, str) and SHA.fullmatch(v) for v in
                    (head.get("sha"), base.get("sha"))))


def ready_candidates(owner: str, repo: str, token: str, seen: set[tuple[str, str]],
                     only_pr: int | None = None) -> list[Candidate]:
    """Freeze new Ready heads using the merge commit GitHub currently reports."""
    result = []
    for page in range(1, 101):
        batch = source_api(f"/repos/{owner}/{repo}/pulls?state=open&per_page=100&page={page}", token)
        if not isinstance(batch, list):
            raise ValueError("incomplete pull request collection")
        for item in batch:
            if not eligible(owner, repo, item):
                continue
            number = item["number"]
            if (repo, item["head"]["sha"]) in seen or (only_pr is not None and number != only_pr):
                continue
            pr = source_api(f"/repos/{owner}/{repo}/pulls/{number}", token)
            merge = pr.get("merge_commit_sha", "")
            if not eligible(owner, repo, pr) or pr.get("mergeable") is not True or not SHA.fullmatch(merge or ""):
                continue
            if (repo, pr["head"]["sha"]) in seen:
                continue
            commit = source_api(f"/repos/{owner}/{repo}/git/commits/{merge}", token)
            parents = [p["sha"] for p in commit.get("parents", [])]
            if commit.get("sha") == merge and parents == [pr["base"]["sha"], pr["head"]["sha"]]:
                result.append(Candidate(repo, number, pr["head"]["sha"], pr["base"]["sha"], merge))
        if len(batch) < 100:
            return result
    raise ValueError("pull request collection exceeds the complete-scan bound")


def seen_runs(own_repo: str, token: str) -> set[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    for page in range(1, RUN_PAGES + 1):
        data = api("GET", f"/repos/{own_repo}/actions/workflows/{WORKFLOW}/runs?per_page=100&page={page}", token)
        runs = data.get("workflow_runs") or []
        for run in runs:
            match = TITLE.match(run.get("display_title") or "")
            if match:
                seen.add((match["repo"], match["sha"]))
        if len(runs) < 100:
            return seen
    raise ValueError("run history exceeds the complete-scan bound")


def snapshot_current(owner: str, candidate: Candidate, token: str) -> bool:
    """Keep the frozen merge while rejecting closed, drafted or changed heads."""
    repo = candidate.repository
    pr = source_api(f"/repos/{owner}/{repo}/pulls/{candidate.number}", token)
    if not eligible(owner, repo, pr) or pr.get("number") != candidate.number or pr["head"]["sha"] != candidate.head:
        return False
    # A rewind or unrelated base changes the meaning of the frozen snapshot.
    current_base = pr["base"]["sha"]
    if current_base != candidate.base:
        comparison = source_api(f"/repos/{owner}/{repo}/compare/{candidate.base}...{current_base}", token)
        if (comparison.get("status") != "ahead" or
                comparison.get("merge_base_commit", {}).get("sha") != candidate.base):
            return False
    elif pr.get("merge_commit_sha") != candidate.merge or pr.get("mergeable") is not True:
        return False
    commit = source_api(f"/repos/{owner}/{repo}/git/commits/{candidate.merge}", token)
    return commit.get("sha") == candidate.merge and [p.get("sha") for p in commit.get("parents", [])] == [candidate.base, candidate.head]


def admit(repo: str, sha: str, base: str, merge: str, pr: str) -> int:
    """Recheck before Windows allocation and again immediately before checkout."""
    if check(repo, sha, base, merge, pr):
        raise ValueError("invalid worker identity")
    own = os.environ["GITHUB_REPOSITORY"]
    owner = own.split("/")[0]
    if own != f"{owner}/hoshi-ci" or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("worker requires its default branch")
    candidate = Candidate(repo, int(pr), sha, base, merge)
    allowed = snapshot_current(owner, candidate, os.environ["SOURCE_TOKEN"])
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write("allowed=" + str(allowed).lower() + "\n")
    print("Ready PR head and frozen merge verified" if allowed else "Source changed or PR is not Ready; no source work")
    return 0 if allowed else 1


def dispatch() -> int:
    source = os.environ.get("SOURCE_TOKEN", "")
    own = os.environ.get("GH_TOKEN", "")
    own_repo = os.environ.get("GITHUB_REPOSITORY", "")
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "") or own_repo.split("/")[0]
    ref = os.environ.get("GITHUB_REF_NAME", "") or DEFAULT_BRANCH
    if not source:
        print("::error::缺少 HOSHIVEL_CI_TOKEN。本倉庫必須可讀此 organization secret，且它須具備來源倉庫的 Contents: read 與 Pull requests: read。")
        return 1
    if not own or not own_repo:
        print("::error::缺少 GH_TOKEN 或 GITHUB_REPOSITORY；這支只在 Actions 裡跑。")
        return 1

    candidates: list[Candidate] = []
    unreadable: list[tuple[str, int]] = []
    if ref != DEFAULT_BRANCH or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("dispatcher requires its default branch")
    manual = os.environ.get("MANUAL_REPO", "")
    manual_pr = os.environ.get("MANUAL_PR", "")
    if manual_pr and (not manual or not manual_pr.isascii() or not manual_pr.isdigit() or int(manual_pr) < 1):
        raise ValueError("manual PR requires an allowed repository and positive number")
    selected = [manual] if manual else load_targets(enabled=True)
    if manual and manual not in load_targets():
        raise ValueError("manual target is not allowed")
    seen = seen_runs(own_repo, own)
    for repo in selected:
        try:
            candidates += ready_candidates(owner, repo, source, seen, int(manual_pr) if manual_pr else None)
        except SourceReadError as err:
            unreadable.append((repo, err.status))
    chosen, deferred = select(candidates, seen, MAX_DISPATCH)
    dry_run = os.environ.get("DRY_RUN", "false") == "true"
    for candidate in chosen:
        if dry_run:
            print(f"Selected {candidate.repository}@{candidate.head}")
            continue
        api("POST", f"/repos/{own_repo}/actions/workflows/{WORKFLOW}/dispatches", own,
            {"ref": ref, "inputs": candidate.inputs()})
        print(f"Dispatched {candidate.repository}@{candidate.head[:12]}")

    print(f"repos={len(selected) - len(unreadable)} new-ready-heads={len(candidates)} selected={len(chosen)} deferred={deferred} dry-run={dry_run}")
    for repo, code in unreadable:
        print(f"::error::Cannot read {owner}/{repo} Ready PR metadata (HTTP {code}). "
              "HOSHIVEL_CI_TOKEN needs repository access with Contents: read and Pull requests: read.")
    return 1 if unreadable else 0


def selftest() -> int:
    import contextlib
    import copy
    import io

    a, b, c, d, e = (letter * 40 for letter in "abcde")
    assert TITLE.match(title("hoshi-data", a))["sha"] == a
    assert not TITLE.match(f"Windows hoshi-data@{a[:12]}")
    assert not TITLE.match(f"Windows hoshi-data@{a.upper()}")
    frozen = Candidate("hoshi-data", 7, a, b, c)
    rebased_snapshot = Candidate("hoshi-data", 7, a, d, e)
    next_head = Candidate("hoshi-data", 7, d, b, e)
    other_repo = Candidate("hoshi-svc", 2, a, b, c)
    candidates = [frozen, rebased_snapshot, next_head, other_repo]
    chosen, deferred = select(candidates, set(), 10)
    assert chosen == [frozen, next_head, other_repo], chosen
    assert deferred == 0
    chosen, deferred = select(candidates, {("hoshi-data", a)}, 1)
    assert chosen == [next_head] and deferred == 1, (chosen, deferred)
    assert frozen.inputs() == {"repo": "hoshi-data", "pr": "7", "sha": a, "base": b, "merge": c}

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "targets.json"
        path.write_text(json.dumps({"windows": ["hoshi-data", "hoshi-svc"]}), encoding="utf-8")
        assert check("hoshi-data", a, b, c, "7", path) is None
        assert check("hoshi-ci", a, b, c, "7", path)
        for bad in (a[:12], a.upper(), "x" * 40, "", "\n" + a):
            assert check("hoshi-data", bad, b, c, "7", path)
            assert check("hoshi-data", a, bad, c, "7", path)
            assert check("hoshi-data", a, b, bad, "7", path)
        for bad in ("0", "-1", "", "1;echo", "１"):
            assert check("hoshi-data", a, b, c, bad, path)
        for bad in ({"windows": []}, {"windows": ["x", "x"]}, {"windows": ["../x"]}, {}):
            path.write_text(json.dumps(bad), encoding="utf-8")
            try:
                load_targets(path)
            except ValueError:
                continue
            raise AssertionError(f"應該拒絕：{bad}")

    load_targets()  # the real list parses
    pr = {"state": "open", "draft": False, "number": 7,
          "mergeable": True, "merge_commit_sha": c,
          "head": {"sha": a, "repo": {"full_name": "Hoshivel/hoshi-data"}},
          "base": {"sha": b, "ref": "main", "repo": {"full_name": "Hoshivel/hoshi-data"}}}
    original = copy.deepcopy(pr)
    assert eligible("Hoshivel", "hoshi-data", pr)
    for key, value in (("draft", True), ("state", "closed")):
        bad = copy.deepcopy(pr); bad[key] = value
        assert not eligible("Hoshivel", "hoshi-data", bad)
    bad = copy.deepcopy(pr); bad["head"]["repo"]["full_name"] = "outside/hoshi-data"
    assert not eligible("Hoshivel", "hoshi-data", bad)
    bad = copy.deepcopy(pr); bad["base"]["ref"] = "other"
    assert not eligible("Hoshivel", "hoshi-data", bad)
    for malformed in (None, [], {}, {**pr, "number": True}, {**pr, "head": []}, {**pr, "base": {"repo": None}}):
        assert not eligible("Hoshivel", "hoshi-data", malformed)
    global api
    saved = api
    calls = []
    dispatched = []
    history = []
    source_error = None
    commit = {"sha": c, "parents": [{"sha": b}, {"sha": a}]}
    comparison = {"status": "ahead", "merge_base_commit": {"sha": b}}
    def fake(method, path, token, body=None):
        calls.append((method, path))
        if method == "POST":
            assert path == "/repos/Hoshivel/hoshi-ci/actions/workflows/windows.yml/dispatches"
            dispatched.append(body)
            return None
        if "/actions/workflows/" in path:
            return {"workflow_runs": history}
        if source_error:
            raise urllib.error.HTTPError(path, source_error, "private response body", {}, None)
        if "pulls?" in path:
            return [pr]
        if "/pulls/" in path:
            return pr
        if "/git/commits/" in path:
            assert path.endswith(c)
            return commit
        if "/compare/" in path:
            return comparison
        raise AssertionError("unexpected API path")
    saved_environment = dict(os.environ)
    try:
        api = fake
        assert ready_candidates("Hoshivel", "hoshi-data", "unused", set()) == [frozen]
        assert snapshot_current("Hoshivel", frozen, "unused")
        calls.clear()
        assert ready_candidates("Hoshivel", "hoshi-data", "unused", {("hoshi-data", a)}) == []
        assert len(calls) == 1, "known heads must not resolve a new merge"
        assert ready_candidates("Hoshivel", "hoshi-data", "unused", set(), 8) == []

        # A normal base advance preserves the frozen merge, without redispatch.
        pr["base"]["sha"] = d
        pr["merge_commit_sha"] = e
        assert snapshot_current("Hoshivel", frozen, "unused")
        assert ready_candidates("Hoshivel", "hoshi-data", "unused", {("hoshi-data", a)}) == []
        comparison["status"] = "behind"
        assert not snapshot_current("Hoshivel", frozen, "unused")
        comparison["status"] = "ahead"
        comparison["merge_base_commit"]["sha"] = e
        assert not snapshot_current("Hoshivel", frozen, "unused")
        comparison["merge_base_commit"]["sha"] = b
        pr = copy.deepcopy(original)
        for key, value in (("draft", True), ("state", "closed"), ("number", 8), ("mergeable", None), ("merge_commit_sha", e)):
            pr[key] = value
            assert not snapshot_current("Hoshivel", frozen, "unused")
            pr = copy.deepcopy(original)
        for section, key, value in (("head", "sha", d), ("base", "ref", "other")):
            pr[section][key] = value
            assert not snapshot_current("Hoshivel", frozen, "unused")
            pr = copy.deepcopy(original)
        pr["head"]["repo"]["full_name"] = "outside/hoshi-data"
        assert not snapshot_current("Hoshivel", frozen, "unused")
        pr = copy.deepcopy(original)
        commit["parents"].reverse()
        assert not snapshot_current("Hoshivel", frozen, "unused")
        assert ready_candidates("Hoshivel", "hoshi-data", "unused", set()) == []
        commit["parents"].reverse()

        os.environ.update({"GITHUB_REPOSITORY": "Hoshivel/hoshi-ci", "GITHUB_REPOSITORY_OWNER": "Hoshivel",
                           "GITHUB_REF": "refs/heads/main", "GITHUB_REF_NAME": "main",
                           "SOURCE_TOKEN": "source", "GH_TOKEN": "own", "MANUAL_REPO": "hoshi-data",
                           "MANUAL_PR": "7", "DRY_RUN": "false"})
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as output:
            os.environ["GITHUB_OUTPUT"] = str(Path(tmp) / "output")
            assert admit("hoshi-data", a, b, c, "7") == 0
            pr["draft"] = True
            assert admit("hoshi-data", a, b, c, "7") == 1
            assert Path(os.environ["GITHUB_OUTPUT"]).read_text() == "allowed=true\nallowed=false\n"
            pr = copy.deepcopy(original)
            assert dispatch() == 0
            assert dispatched == [{"ref": "main", "inputs": frozen.inputs()}]
            history[:] = [{"display_title": title("hoshi-data", a), "conclusion": "failure"}]
            assert dispatch() == 0
            assert len(dispatched) == 1, "failed admissions require explicit retry"
            history.clear()
            os.environ["DRY_RUN"] = "true"
            assert dispatch() == 0
            assert len(dispatched) == 1
            source_error = 403
            assert dispatch() == 1
            assert main(["admit", "--repo", "hoshi-data", "--sha", a, "--base", b, "--merge", c, "--pr", "7"]) == 1
            assert "Pull requests: read" in output.getvalue()
            assert "private response body" not in output.getvalue()
    finally:
        api = saved
        os.environ.clear()
        os.environ.update(saved_environment)
    print("dispatch.py selftest：全部通過")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode")
    for mode in ("check", "admit"):
        command = sub.add_parser(mode)
        for argument in ("repo", "sha", "base", "merge", "pr"):
            command.add_argument(f"--{argument}", required=True)
    sub.add_parser("selftest")
    args = parser.parse_args(argv)

    if args.mode == "selftest":
        return selftest()
    if args.mode == "check":
        reason = check(args.repo, args.sha, args.base, args.merge, args.pr)
        if reason:
            print(f"::error::{reason}")
            return 1
        return 0
    try:
        if args.mode == "admit":
            return admit(args.repo, args.sha, args.base, args.merge, args.pr)
        return dispatch()
    except SourceReadError as error:
        print(f"::error::Source Ready PR metadata could not be read (HTTP {error.status}). "
              "HOSHIVEL_CI_TOKEN needs repository access with Contents: read and Pull requests: read.")
    except urllib.error.HTTPError as error:
        print(f"::error::GitHub API rejected the operation (HTTP {error.code})")
    except (KeyError, ValueError, urllib.error.URLError):
        print("::error::Invalid input, missing credentials, or incomplete source metadata")
    return 1


if __name__ == "__main__":
    sys.exit(main())
