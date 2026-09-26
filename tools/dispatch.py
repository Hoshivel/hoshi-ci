#!/usr/bin/env python3
"""替還沒驗過的每個分支 head 派一次 Windows 驗證。

由本倉庫的排程觸發。被驗證的倉庫是私有的，而且不持有任何指向這裡的憑證：
hoshi-ci 主動去拉，沒有人往這裡推。

對 `targets.json` 的 `windows` 清單裡每個倉庫，以 HOSHIVEL_CI_TOKEN（唯讀）列出
每條分支的 head，丟掉已經有 `windows.yml` run 的 SHA（不論結果），其餘每個 SHA
以本倉庫自己的 GITHUB_TOKEN 派一個 run。run 的標題是 `Windows <倉庫>@<sha>`，
這個標題就是「跑過什麼」的唯一記錄，別處沒有狀態。

分支名只讀不印：日誌只寫倉庫與 commit。

用法：
  dispatch.py                         派工（需要 SOURCE_TOKEN、GH_TOKEN、GITHUB_REPOSITORY）
  dispatch.py check --repo R --sha S  檢查 windows.yml 的輸入是否合法
  dispatch.py selftest                只跑內建案例

只用標準庫。
"""

from __future__ import annotations

import argparse
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
MAX_DISPATCH = 24
# How much of the run history is read (100 runs a page). A head that is still
# a head but older than this gets one more run: harmless, and the lookup stays
# bounded.
RUN_PAGES = 5


def title(repo: str, sha: str) -> str:
    return f"Windows {repo}@{sha}"


def load_targets(path: Path = TARGETS) -> list[str]:
    repos = json.loads(path.read_text(encoding="utf-8")).get("windows")
    if not isinstance(repos, list) or not repos or not all(isinstance(r, str) and REPO.match(r) for r in repos):
        raise ValueError(f"{path.name} 的 windows 必須是非空的倉庫名清單")
    if len(set(repos)) != len(repos):
        raise ValueError(f"{path.name} 的 windows 有重複")
    return repos


def check(repo: str, sha: str, path: Path = TARGETS) -> str | None:
    """Return the reason the inputs are rejected, or None."""
    if repo not in load_targets(path):
        return f"{repo!r} 不在 {path.name} 的 windows 清單"
    if not SHA.match(sha):
        return "sha 必須是 40 字元的小寫十六進位"
    return None


def select(heads: dict[str, list[tuple[str, str]]], seen: set[tuple[str, str]],
           limit: int) -> tuple[list[tuple[str, str]], int]:
    """Pick (repo, sha) pairs without a run: default branches first, each commit once."""
    ordered = sorted(
        (branch != DEFAULT_BRANCH, repo, sha)
        for repo, branches in heads.items()
        for branch, sha in branches
    )
    pending: list[tuple[str, str]] = []
    for _, repo, sha in ordered:
        key = (repo, sha)
        if key not in seen and key not in pending:
            pending.append(key)
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


def branch_heads(owner: str, repo: str, token: str) -> list[tuple[str, str]]:
    heads: list[tuple[str, str]] = []
    page = 1
    while True:
        batch = api("GET", f"/repos/{owner}/{repo}/branches?per_page=100&page={page}", token)
        heads += [(b["name"], b["commit"]["sha"]) for b in batch]
        if len(batch) < 100:
            return heads
        page += 1


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
            break
    return seen


def dispatch() -> int:
    source = os.environ.get("SOURCE_TOKEN", "")
    own = os.environ.get("GH_TOKEN", "")
    own_repo = os.environ.get("GITHUB_REPOSITORY", "")
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "") or own_repo.split("/")[0]
    ref = os.environ.get("GITHUB_REF_NAME", "") or DEFAULT_BRANCH
    if not source:
        print("::error::缺少 HOSHIVEL_CI_TOKEN。它是 organization secret，本倉庫要在它的可見範圍內；權限只需要 Contents: Read-only。")
        return 1
    if not own or not own_repo:
        print("::error::缺少 GH_TOKEN 或 GITHUB_REPOSITORY；這支只在 Actions 裡跑。")
        return 1

    heads: dict[str, list[tuple[str, str]]] = {}
    unreadable: list[tuple[str, int]] = []
    for repo in load_targets():
        try:
            heads[repo] = branch_heads(owner, repo, source)
        except urllib.error.HTTPError as err:
            unreadable.append((repo, err.code))

    seen = seen_runs(own_repo, own)
    chosen, deferred = select(heads, seen, MAX_DISPATCH)
    for repo, sha in chosen:
        api("POST", f"/repos/{own_repo}/actions/workflows/{WORKFLOW}/dispatches", own,
            {"ref": ref, "inputs": {"repo": repo, "sha": sha}})
        print(f"派出 {repo}@{sha[:12]}")

    total = sum(len(v) for v in heads.values())
    print(f"{len(heads)} 個倉庫、{total} 個分支 head；派出 {len(chosen)} 個，留到下一輪 {deferred} 個，其餘已有 run。")
    for repo, code in unreadable:
        print(f"::error::讀不到 {owner}/{repo} 的分支（HTTP {code}）。HOSHIVEL_CI_TOKEN 的 Repository access "
              "要包含它，權限 Contents: Read-only。私有倉庫讀不到時回的是 404，不是 403。")
    return 1 if unreadable else 0


def selftest() -> int:
    a, b, c = "a" * 40, "b" * 40, "c" * 40
    assert TITLE.match(title("hoshi-data", a))["sha"] == a
    assert not TITLE.match(f"Windows hoshi-data@{a[:12]}")
    assert not TITLE.match(f"Windows hoshi-data@{a.upper()}")

    heads = {
        "r1": [("feature", a), (DEFAULT_BRANCH, b)],
        "r2": [(DEFAULT_BRANCH, a), ("fresh", a), ("other", c)],
    }
    chosen, deferred = select(heads, set(), 10)
    # Default branches first; a commit shared by two branches of one repo once.
    assert chosen == [("r1", b), ("r2", a), ("r1", a), ("r2", c)], chosen
    assert deferred == 0
    chosen, deferred = select(heads, {("r1", b), ("r2", c)}, 1)
    assert chosen == [("r2", a)] and deferred == 1, (chosen, deferred)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "targets.json"
        path.write_text(json.dumps({"windows": ["hoshi-data", "hoshi-svc"]}), encoding="utf-8")
        assert check("hoshi-data", a, path) is None
        assert check("hoshi-ci", a, path)
        assert check("hoshi-data", a[:12], path)
        assert check("hoshi-data", a.upper(), path)
        for bad in ({"windows": []}, {"windows": ["x", "x"]}, {"windows": ["../x"]}, {}):
            path.write_text(json.dumps(bad), encoding="utf-8")
            try:
                load_targets(path)
            except ValueError:
                continue
            raise AssertionError(f"應該拒絕：{bad}")

    load_targets()  # the real list parses
    print("dispatch.py selftest：全部通過")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode")
    checker = sub.add_parser("check")
    checker.add_argument("--repo", required=True)
    checker.add_argument("--sha", required=True)
    sub.add_parser("selftest")
    args = parser.parse_args(argv)

    if args.mode == "selftest":
        return selftest()
    if args.mode == "check":
        reason = check(args.repo, args.sha)
        if reason:
            print(f"::error::{reason}")
            return 1
        return 0
    return dispatch()


if __name__ == "__main__":
    sys.exit(main())
