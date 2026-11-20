#!/usr/bin/env python3
"""為尚未驗證的 Ready PR 合併提交派一次 Windows 驗證。

由本倉庫的排程觸發。被驗證的倉庫是私有的，而且不持有任何指向這裡的憑證：
hoshi-ci 主動去拉，沒有人往這裡推。

對 `targets.json` 的 `windows` 清單裡每個倉庫，以 HOSHIVEL_CI_TOKEN（唯讀）列出
同倉 Ready PR 的精確合併提交，丟掉已經有 `windows.yml` run 的 SHA（不論結果），其餘每個 SHA
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
MAX_DISPATCH = 2
# Read complete history within this bound. Refuse an incomplete scan rather
# than quietly redispatch a revision whose old result has fallen off the page.
RUN_PAGES = 100


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


def eligible(owner: str, repo: str, pr: dict) -> bool:
    source = f"{owner}/{repo}"
    head, base = pr.get("head") or {}, pr.get("base") or {}
    return (pr.get("state") == "open" and pr.get("draft") is False
            and (head.get("repo") or {}).get("full_name") == source
            and (base.get("repo") or {}).get("full_name") == source
            and base.get("ref") == DEFAULT_BRANCH
            and all(isinstance(v, str) and SHA.fullmatch(v) for v in
                    (head.get("sha"), base.get("sha"))))


def branch_heads(owner: str, repo: str, token: str) -> list[tuple[str, str]]:
    """Return Ready PR merge commits whose two parents match current metadata."""
    result = []
    for page in range(1, 101):
        batch = api("GET", f"/repos/{owner}/{repo}/pulls?state=open&per_page=100&page={page}", token)
        if not isinstance(batch, list):
            raise ValueError("incomplete pull request collection")
        for item in batch:
            if not eligible(owner, repo, item):
                continue
            number = item["number"]
            pr = api("GET", f"/repos/{owner}/{repo}/pulls/{number}", token)
            merge = pr.get("merge_commit_sha", "")
            if not eligible(owner, repo, pr) or pr.get("mergeable") is not True or not SHA.fullmatch(merge or ""):
                continue
            commit = api("GET", f"/repos/{owner}/{repo}/commits/{merge}", token)
            parents = [p["sha"] for p in commit.get("parents", [])]
            if commit.get("sha") == merge and parents == [pr["base"]["sha"], pr["head"]["sha"]]:
                result.append((str(number), merge))
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


def admit(repo: str, sha: str) -> int:
    """Recheck before Windows allocation and again immediately before checkout."""
    if check(repo, sha):
        raise ValueError("invalid worker identity")
    own = os.environ["GITHUB_REPOSITORY"]
    owner = own.split("/")[0]
    if own != f"{owner}/hoshi-ci" or os.environ.get("GITHUB_REF_NAME") != DEFAULT_BRANCH:
        raise ValueError("worker requires its default branch")
    allowed = sha in {value for _, value in branch_heads(owner, repo, os.environ["SOURCE_TOKEN"])}
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write("allowed=" + str(allowed).lower() + "\n")
    print("Ready PR merge verified" if allowed else "Source changed or PR is not Ready; no source work")
    return 0 if allowed else 1


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
    if ref != DEFAULT_BRANCH:
        raise ValueError("dispatcher requires its default branch")
    manual = os.environ.get("MANUAL_REPO", "")
    manual_pr = os.environ.get("MANUAL_PR", "")
    if manual_pr and (not manual or not manual_pr.isascii() or not manual_pr.isdigit() or int(manual_pr) < 1):
        raise ValueError("manual PR requires an allowed repository and positive number")
    selected = [manual] if manual else load_targets(enabled=True)
    if manual and manual not in load_targets():
        raise ValueError("manual target is not allowed")
    for repo in selected:
        try:
            heads[repo] = branch_heads(owner, repo, source)
            if manual_pr:
                heads[repo] = [(number, sha) for number, sha in heads[repo] if number == manual_pr]
        except urllib.error.HTTPError as err:
            unreadable.append((repo, err.code))

    seen = seen_runs(own_repo, own)
    chosen, deferred = select(heads, seen, MAX_DISPATCH)
    dry_run = os.environ.get("DRY_RUN", "false") == "true"
    for repo, sha in chosen:
        if dry_run:
            print(f"Selected {repo}@{sha}")
            continue
        api("POST", f"/repos/{own_repo}/actions/workflows/{WORKFLOW}/dispatches", own,
            {"ref": ref, "inputs": {"repo": repo, "sha": sha}})
        print(f"派出 {repo}@{sha[:12]}")

    total = sum(len(v) for v in heads.values())
    print(f"repos={len(heads)} ready-merges={total} selected={len(chosen)} deferred={deferred} dry-run={dry_run}")
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
    import copy
    pr = {"state": "open", "draft": False, "number": 7,
          "head": {"sha": a, "repo": {"full_name": "Hoshivel/hoshi-data"}},
          "base": {"sha": b, "ref": "main", "repo": {"full_name": "Hoshivel/hoshi-data"}}}
    assert eligible("Hoshivel", "hoshi-data", pr)
    for key, value in (("draft", True), ("state", "closed")):
        bad = copy.deepcopy(pr); bad[key] = value
        assert not eligible("Hoshivel", "hoshi-data", bad)
    bad = copy.deepcopy(pr); bad["head"]["repo"]["full_name"] = "outside/hoshi-data"
    assert not eligible("Hoshivel", "hoshi-data", bad)
    bad = copy.deepcopy(pr); bad["base"]["ref"] = "other"
    assert not eligible("Hoshivel", "hoshi-data", bad)
    global api
    saved = api
    def fake(method, path, token, body=None):
        if "pulls?" in path:
            return [pr]
        if "/pulls/" in path:
            return {**pr, "mergeable": True, "merge_commit_sha": c}
        return {"sha": c, "parents": [{"sha": b}, {"sha": a}]}
    try:
        api = fake
        assert branch_heads("Hoshivel", "hoshi-data", "unused") == [("7", c)]
        pr["base"]["sha"] = c
        assert branch_heads("Hoshivel", "hoshi-data", "unused") == []
    finally:
        api = saved
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
    admission = sub.add_parser("admit")
    admission.add_argument("--repo", required=True)
    admission.add_argument("--sha", required=True)
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
    try:
        if args.mode == "admit":
            return admit(args.repo, args.sha)
        return dispatch()
    except urllib.error.HTTPError as error:
        print(f"::error::GitHub API rejected the operation (HTTP {error.code})")
    except (KeyError, ValueError, urllib.error.URLError):
        print("::error::Invalid input, missing credentials, or incomplete source metadata")
    return 1


if __name__ == "__main__":
    sys.exit(main())
