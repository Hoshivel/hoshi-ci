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
PROFILES = {
    "windows": (WORKFLOW, "Windows", "windows", "enabled_windows"),
    "cdp-windows": ("cdp-windows.yml", "Windows CDP", "cdp_windows", "enabled_cdp_windows"),
}

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


def title(repo: str, sha: str, profile: str = "windows") -> str:
    return f"{PROFILES[profile][1]} {repo}@{sha}"


def load_targets(path: Path = TARGETS, *, enabled: bool = False,
                 profile: str = "windows") -> list[str]:
    if profile not in PROFILES:
        raise ValueError("unknown execution profile")
    _, _, key, enabled_key = PROFILES[profile]
    document = json.loads(path.read_text(encoding="utf-8"))
    repos = document.get(key, [] if profile == "cdp-windows" else None)
    if not isinstance(repos, list) or (profile == "windows" and not repos) or not all(isinstance(r, str) and REPO.fullmatch(r) for r in repos):
        raise ValueError("invalid execution profile repository list")
    if profile == "cdp-windows" and any(repo != "ShatteredRealms" for repo in repos):
        raise ValueError("CDP Windows only supports ShatteredRealms")
    if len(set(repos)) != len(repos):
        raise ValueError(f"{path.name} 的 windows 有重複")
    if not enabled:
        return repos
    active = document.get(enabled_key, [])
    if not isinstance(active, list) or any(r not in repos for r in active) or len(set(active)) != len(active):
        raise ValueError("invalid enabled Windows list")
    return active


def check(repo: str, sha: str, base: str, merge: str, pr: str,
          path: Path = TARGETS, *, profile: str = "windows") -> str | None:
    """Return the reason the inputs are rejected, or None."""
    if repo not in load_targets(path, profile=profile):
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


def main_tip(source: str, token: str) -> str:
    ref = f"refs/heads/{DEFAULT_BRANCH}"
    result = source_api(f"{source}/git/ref/heads/{DEFAULT_BRANCH}", token)
    target = result.get("object") if isinstance(result, dict) and result.get("ref") == ref else None
    sha = target.get("sha") if isinstance(target, dict) and target.get("type") == "commit" else None
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ValueError("incomplete main ref")
    return sha


def is_ancestor(source: str, ancestor: str, descendant: str, token: str) -> bool:
    if ancestor == descendant:
        return True
    comparison = source_api(f"{source}/compare/{ancestor}...{descendant}?per_page=1", token)
    if not isinstance(comparison, dict) or comparison.get("status") != "ahead":
        return False
    return all(isinstance(comparison.get(key), dict) and comparison[key].get("sha") == ancestor
               for key in ("base_commit", "merge_base_commit"))


def merge_parents(source: str, merge: str, token: str) -> tuple[str, str] | None:
    commit = source_api(f"{source}/git/commits/{merge}", token)
    if not isinstance(commit, dict) or commit.get("sha") != merge:
        return None
    parents = commit.get("parents")
    if not isinstance(parents, list) or len(parents) != 2:
        return None
    parent_shas = [parent.get("sha") if isinstance(parent, dict) else None for parent in parents]
    if parent_shas[0] == parent_shas[1] or not all(isinstance(sha, str) and SHA.fullmatch(sha) for sha in parent_shas):
        return None
    return parent_shas[0], parent_shas[1]


def ready_candidates(owner: str, repo: str, token: str, seen: set[tuple[str, str]],
                     only_pr: int | None = None) -> list[Candidate]:
    """Freeze new Ready heads using the merge commit GitHub currently reports."""
    source = f"/repos/{owner}/{repo}"
    result = []
    for page in range(1, 101):
        batch = source_api(f"{source}/pulls?state=open&per_page=100&page={page}", token)
        if not isinstance(batch, list):
            raise ValueError("incomplete pull request collection")
        for item in batch:
            if not eligible(owner, repo, item):
                continue
            number = item["number"]
            if (repo, item["head"]["sha"]) in seen or (only_pr is not None and number != only_pr):
                continue
            pr = source_api(f"{source}/pulls/{number}", token)
            if not eligible(owner, repo, pr) or pr["number"] != number or pr.get("mergeable") is not True:
                continue
            merge, head = pr.get("merge_commit_sha"), pr["head"]["sha"]
            if not isinstance(merge, str) or not SHA.fullmatch(merge) or (repo, head) in seen:
                continue
            parents = merge_parents(source, merge, token)
            if parents is None or parents[1] != head:
                continue
            # PR metadata can lag the base used to generate GitHub's merge commit.
            base = parents[0]
            if (is_ancestor(source, pr["base"]["sha"], base, token)
                    and is_ancestor(source, base, main_tip(source, token), token)):
                result.append(Candidate(repo, number, head, base, merge))
        if len(batch) < 100:
            return result
    raise ValueError("pull request collection exceeds the complete-scan bound")


def seen_runs(own_repo: str, token: str, profile: str = "windows") -> set[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    workflow, prefix, _, _ = PROFILES[profile]
    pattern = re.compile(r"^" + re.escape(prefix) + r" (?P<repo>[A-Za-z0-9._-]+)@(?P<sha>[0-9a-f]{40})$")
    for page in range(1, RUN_PAGES + 1):
        data = api("GET", f"/repos/{own_repo}/actions/workflows/{workflow}/runs?per_page=100&page={page}", token)
        runs = data.get("workflow_runs") or []
        for run in runs:
            match = pattern.fullmatch(run.get("display_title") or "")
            if match:
                seen.add((match["repo"], match["sha"]))
        if len(runs) < 100:
            return seen
    raise ValueError("run history exceeds the complete-scan bound")


def snapshot_current(owner: str, candidate: Candidate, token: str,
                     profile: str = "windows") -> bool:
    """Keep the frozen merge while rejecting closed, drafted or changed heads."""
    if profile not in PROFILES:
        raise ValueError("unknown execution profile")
    repo = candidate.repository
    source = f"/repos/{owner}/{repo}"
    pr = source_api(f"{source}/pulls/{candidate.number}", token)
    if not eligible(owner, repo, pr) or pr["number"] != candidate.number or pr["head"]["sha"] != candidate.head:
        return False
    current_base = main_tip(source, token)
    # CDP accepts only the exact merge GitHub currently associates with this
    # Ready PR and current main. Two parent SHAs alone cannot identify its tree.
    if profile == "cdp-windows" and (current_base != candidate.base
            or pr.get("merge_commit_sha") != candidate.merge or pr.get("mergeable") is not True):
        return False
    if not is_ancestor(source, candidate.base, current_base, token):
        return False
    if current_base == candidate.base and (pr.get("merge_commit_sha") != candidate.merge or pr.get("mergeable") is not True):
        return False
    return merge_parents(source, candidate.merge, token) == (candidate.base, candidate.head)


def admit(repo: str, sha: str, base: str, merge: str, pr: str,
          profile: str = "windows") -> int:
    """Recheck before Windows allocation and again immediately before checkout."""
    if check(repo, sha, base, merge, pr, profile=profile):
        raise ValueError("invalid worker identity")
    own = os.environ["GITHUB_REPOSITORY"]
    owner = own.split("/")[0]
    if own != f"{owner}/hoshi-ci" or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("worker requires its default branch")
    candidate = Candidate(repo, int(pr), sha, base, merge)
    allowed = snapshot_current(owner, candidate, os.environ["SOURCE_TOKEN"], profile)
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

    queues: dict[str, list[Candidate]] = {}
    unreadable: list[tuple[str, int]] = []
    if ref != DEFAULT_BRANCH or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("dispatcher requires its default branch")
    manual = os.environ.get("MANUAL_REPO", "")
    manual_pr = os.environ.get("MANUAL_PR", "")
    if manual_pr and (not manual or not manual_pr.isascii() or not manual_pr.isdigit() or int(manual_pr) < 1):
        raise ValueError("manual PR requires an allowed repository and positive number")
    requested = os.environ.get("MANUAL_PROFILE", "")
    if requested and requested not in PROFILES:
        raise ValueError("unknown execution profile")
    profiles = [requested or "windows"] if requested or manual else list(PROFILES)
    repo_count = 0
    for profile in profiles:
        selected = [manual] if manual else load_targets(enabled=True, profile=profile)
        if manual and manual not in load_targets(profile=profile):
            raise ValueError("manual target is not allowed")
        if not selected:
            continue
        repo_count += len(selected)
        seen = seen_runs(own_repo, own, profile)
        candidates: list[Candidate] = []
        for repo in selected:
            try:
                ready = ready_candidates(owner, repo, source, seen, int(manual_pr) if manual_pr else None)
                if profile == "cdp-windows":
                    ready = [candidate for candidate in ready
                             if snapshot_current(owner, candidate, source, profile)]
                candidates += ready
            except SourceReadError as err:
                unreadable.append((repo, err.status))
        queues[profile] = select(candidates, seen, len(candidates))[0]
    # Alternate routes within one aggregate budget. Ordinary Windows results
    # must neither suppress CDP work nor give it a second full budget.
    pending = [(profile, queue[index]) for index in range(max(map(len, queues.values()), default=0))
               for profile, queue in queues.items() if index < len(queue)]
    chosen = pending[:MAX_DISPATCH]
    deferred = max(0, len(pending) - MAX_DISPATCH)
    dry_run = os.environ.get("DRY_RUN", "false") == "true"
    for profile, candidate in chosen:
        if dry_run:
            print(f"Selected {profile} {candidate.repository}@{candidate.head}")
            continue
        workflow = PROFILES[profile][0]
        api("POST", f"/repos/{own_repo}/actions/workflows/{workflow}/dispatches", own,
            {"ref": ref, "inputs": candidate.inputs()})
        print(f"Dispatched {profile} {candidate.repository}@{candidate.head[:12]}")

    print(f"repos={repo_count - len(unreadable)} new-ready-heads={len(pending)} selected={len(chosen)} deferred={deferred} dry-run={dry_run}")
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
        profile_targets = {"windows": ["hoshi-data", "ShatteredRealms"],
                           "enabled_windows": ["hoshi-data"],
                           "cdp_windows": ["ShatteredRealms"],
                           "enabled_cdp_windows": ["ShatteredRealms"]}
        path.write_text(json.dumps(profile_targets), encoding="utf-8")
        assert load_targets(path, profile="cdp-windows") == ["ShatteredRealms"]
        assert load_targets(path, enabled=True, profile="cdp-windows") == ["ShatteredRealms"]
        assert check("ShatteredRealms", a, b, c, "7", path, profile="cdp-windows") is None
        assert check("hoshi-data", a, b, c, "7", path, profile="cdp-windows")
        for change in ({"cdp_windows": ["hoshi-data"]},
                       {"cdp_windows": ["ShatteredRealms", "ShatteredRealms"]},
                       {"cdp_windows": ["ShatteredRealms\n"]},
                       {"enabled_cdp_windows": ["hoshi-data"]},
                       {"enabled_cdp_windows": ["ShatteredRealms", "ShatteredRealms"]}):
            path.write_text(json.dumps({**profile_targets, **change}), encoding="utf-8")
            try:
                load_targets(path, enabled=True, profile="cdp-windows")
            except ValueError:
                continue
            raise AssertionError("unsupported CDP allowlist was accepted")
        try:
            load_targets(path, profile="unknown")
        except ValueError:
            pass
        else:
            raise AssertionError("unknown profile was accepted")
        path.write_text(json.dumps({"windows": ["hoshi-data"]}), encoding="utf-8")
        assert load_targets(path, enabled=True, profile="cdp-windows") == []

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
    failed_read = ""
    commit = {"sha": c, "parents": [{"sha": b}, {"sha": a}]}
    original_commit = copy.deepcopy(commit)
    tip = b
    comparisons = {(older, newer): {"status": "ahead", "base_commit": {"sha": older},
                                    "merge_base_commit": {"sha": older}}
                   for older, newer in ((b, d), (d, e))}
    def fake(method, path, token, body=None):
        calls.append((method, path))
        if method == "POST":
            assert path == "/repos/Hoshivel/hoshi-ci/actions/workflows/windows.yml/dispatches"
            dispatched.append(body)
            return None
        if "/actions/workflows/" in path:
            return {"workflow_runs": history}
        if source_error and failed_read in path:
            raise urllib.error.HTTPError(path, source_error, "private response body", {}, None)
        if "pulls?" in path:
            return [pr]
        if "/pulls/" in path:
            return pr
        if "/git/commits/" in path:
            assert path.endswith(c)
            return commit
        if path.endswith("/git/ref/heads/main"):
            return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": tip}}
        if "/compare/" in path:
            pair = tuple(path.removesuffix("?per_page=1").split("/compare/")[1].split("..."))
            return comparisons.get(pair, {"status": "diverged", "merge_base_commit": {"sha": a}})
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

        # GitHub used d for the merge while the PR API still reports b.
        tip = d
        commit["parents"][0]["sha"] = d
        actual = Candidate("hoshi-data", 7, a, d, c)
        assert ready_candidates("Hoshivel", "hoshi-data", "unused", set()) == [actual]
        assert snapshot_current("Hoshivel", actual, "unused")
        tip = b
        assert ready_candidates("Hoshivel", "hoshi-data", "unused", set()) == []
        assert not snapshot_current("Hoshivel", actual, "unused")
        tip = e
        assert ready_candidates("Hoshivel", "hoshi-data", "unused", set()) == [actual]
        assert snapshot_current("Hoshivel", actual, "unused")
        pr["base"]["sha"] = e
        assert ready_candidates("Hoshivel", "hoshi-data", "unused", set()) == []

        # A normal base advance preserves the frozen merge even with a stale PR base.
        commit = copy.deepcopy(original_commit)
        pr = copy.deepcopy(original)
        tip = d
        pr["merge_commit_sha"] = e
        assert snapshot_current("Hoshivel", frozen, "unused")
        assert ready_candidates("Hoshivel", "hoshi-data", "unused", {("hoshi-data", a)}) == []
        comparisons[(b, d)]["status"] = "behind"
        assert not snapshot_current("Hoshivel", frozen, "unused")
        comparisons[(b, d)]["status"] = "ahead"
        comparisons[(b, d)]["merge_base_commit"]["sha"] = e
        assert not snapshot_current("Hoshivel", frozen, "unused")
        comparisons[(b, d)]["merge_base_commit"]["sha"] = b
        comparisons[(b, d)]["base_commit"]["sha"] = e
        assert not snapshot_current("Hoshivel", frozen, "unused")
        comparisons[(b, d)]["base_commit"]["sha"] = b
        tip = b
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
        for parents in ([{"sha": a}, {"sha": b}], [{"sha": b}], [{"sha": b}, {"sha": a}, {"sha": d}],
                        [{"sha": a}, {"sha": a}], [{"sha": b}, None], [{"sha": "invalid"}, {"sha": a}]):
            commit["parents"] = parents
            assert not snapshot_current("Hoshivel", frozen, "unused")
            assert ready_candidates("Hoshivel", "hoshi-data", "unused", set()) == []
        commit = copy.deepcopy(original_commit)
        commit["sha"] = e
        assert not snapshot_current("Hoshivel", frozen, "unused")
        assert ready_candidates("Hoshivel", "hoshi-data", "unused", set()) == []
        commit = copy.deepcopy(original_commit)

        os.environ.update({"GITHUB_REPOSITORY": "Hoshivel/hoshi-ci", "GITHUB_REPOSITORY_OWNER": "Hoshivel",
                           "GITHUB_REF": "refs/heads/main", "GITHUB_REF_NAME": "main",
                           "SOURCE_TOKEN": "source", "GH_TOKEN": "own", "MANUAL_REPO": "hoshi-data",
                           "MANUAL_PR": "7", "MANUAL_PROFILE": "", "DRY_RUN": "false"})
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
            os.environ["DRY_RUN"] = "false"
            source_error = 403
            tip = d
            for failed_read in ("/pulls", "/git/ref/", "/git/commits/", "/compare/"):
                assert dispatch() == 1
                assert main(["admit", "--repo", "hoshi-data", "--sha", a, "--base", b, "--merge", c, "--pr", "7"]) == 1
                assert len(dispatched) == 1
            assert "Pull requests: read" in output.getvalue()
            assert "private response body" not in output.getvalue()
    finally:
        api = saved
        os.environ.clear()
        os.environ.update(saved_environment)

    # Exercise both routes through the real provenance and selection code.
    # Histories are separate even when the repository and Ready head match.
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        profile_path = Path(tmp) / "targets.json"
        profile_path.write_text(json.dumps({
            "windows": ["ShatteredRealms", "hoshi-data"],
            "enabled_windows": ["ShatteredRealms", "hoshi-data"],
            "cdp_windows": ["ShatteredRealms"],
            "enabled_cdp_windows": ["ShatteredRealms"],
        }), encoding="utf-8")
        real_load_targets = load_targets

        def fixture_targets(path=profile_path, *, enabled=False, profile="windows"):
            return real_load_targets(profile_path, enabled=enabled, profile=profile)

        def fixture_pr(repo, number, head, merge):
            item = copy.deepcopy(original)
            item.update(number=number, merge_commit_sha=merge)
            item["head"].update(sha=head, repo={"full_name": f"Hoshivel/{repo}"})
            item["base"].update(repo={"full_name": f"Hoshivel/{repo}"})
            return item

        fixtures = {("ShatteredRealms", 7): fixture_pr("ShatteredRealms", 7, a, c),
                    ("ShatteredRealms", 8): fixture_pr("ShatteredRealms", 8, d, "f" * 40),
                    ("hoshi-data", 9): fixture_pr("hoshi-data", 9, e, "1" * 40)}
        histories = {"windows.yml": [], "cdp-windows.yml": []}
        posts = []
        reads = []
        workflow_reads = []
        live_tip = b
        extra_commits = {}

        def multi_api(method, path, token, body=None):
            if "/actions/workflows/" in path:
                workflow = path.split("/actions/workflows/", 1)[1].split("/", 1)[0]
                assert workflow in histories, workflow
                assert token == "own", "source token must not dispatch public work"
                if method == "POST":
                    assert body["ref"] == "main"
                    posts.append((workflow, copy.deepcopy(body)))
                    profile = "cdp-windows" if workflow == "cdp-windows.yml" else "windows"
                    inputs = body["inputs"]
                    histories[workflow].append({"display_title": title(inputs["repo"], inputs["sha"], profile)})
                    return None
                assert method == "GET"
                workflow_reads.append(workflow)
                return {"workflow_runs": copy.deepcopy(histories[workflow])}
            assert method == "GET" and token == "source"
            reads.append(path)
            repo = path.split("/")[3]
            if "pulls?" in path:
                return [item for (source_repo, _), item in fixtures.items() if source_repo == repo]
            if "/pulls/" in path:
                return fixtures[(repo, int(path.rsplit("/", 1)[1]))]
            if path.endswith("/git/ref/heads/main"):
                return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": live_tip}}
            if "/git/commits/" in path:
                merge = path.rsplit("/", 1)[1]
                if merge in extra_commits:
                    return extra_commits[merge]
                item = next(item for (source_repo, _), item in fixtures.items()
                            if source_repo == repo and item["merge_commit_sha"] == merge)
                return {"sha": merge, "parents": [{"sha": b}, {"sha": item["head"]["sha"]}]}
            if "/compare/" in path:
                assert path.endswith(f"/compare/{b}...{d}?per_page=1")
                return {"status": "ahead", "base_commit": {"sha": b}, "merge_base_commit": {"sha": b}}
            raise AssertionError("unexpected profile API path")

        environment = {"GITHUB_REPOSITORY": "Hoshivel/hoshi-ci", "GITHUB_REPOSITORY_OWNER": "Hoshivel",
                       "GITHUB_REF": "refs/heads/main", "GITHUB_REF_NAME": "main",
                       "SOURCE_TOKEN": "source", "GH_TOKEN": "own", "MANUAL_REPO": "",
                       "MANUAL_PR": "", "MANUAL_PROFILE": "", "DRY_RUN": "false",
                       "GITHUB_OUTPUT": str(Path(tmp) / "output")}
        with patch.object(sys.modules[__name__], "api", multi_api), \
             patch.object(sys.modules[__name__], "load_targets", fixture_targets), \
             patch.dict(os.environ, environment, clear=True), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            sr_frozen = Candidate("ShatteredRealms", 7, a, b, c)
            assert snapshot_current("Hoshivel", sr_frozen, "source", "cdp-windows")
            live_tip = d
            # A valid frozen merge remains compatible with ordinary Windows,
            # while CDP requires GitHub's current main and merge binding.
            assert snapshot_current("Hoshivel", sr_frozen, "source")
            assert not snapshot_current("Hoshivel", sr_frozen, "source", "cdp-windows")
            assert admit("ShatteredRealms", a, b, c, "7") == 0
            assert admit("ShatteredRealms", a, b, c, "7", "cdp-windows") == 1
            fabricated_sha = "9" * 40
            extra_commits[fabricated_sha] = {
                "sha": fabricated_sha, "parents": [{"sha": b}, {"sha": a}],
                "tree": {"sha": "8" * 40},
            }
            fabricated = Candidate("ShatteredRealms", 7, a, b, fabricated_sha)
            assert snapshot_current("Hoshivel", fabricated, "source")
            assert not snapshot_current("Hoshivel", fabricated, "source", "cdp-windows")
            assert admit("ShatteredRealms", a, b, fabricated_sha, "7", "cdp-windows") == 1
            # The CDP dispatcher must reject stale snapshots for both named
            # pilots and automatic ticks, before any worker is requested.
            os.environ.update(MANUAL_REPO="ShatteredRealms", MANUAL_PR="7", MANUAL_PROFILE="cdp-windows")
            assert dispatch() == 0 and not posts
            os.environ.update(MANUAL_REPO="", MANUAL_PR="", MANUAL_PROFILE="")
            assert dispatch() == 0 and len(posts) == 2
            assert all(workflow == "windows.yml" for workflow, _ in posts)
            live_tip = b
            assert snapshot_current("Hoshivel", sr_frozen, "source", "cdp-windows")
            assert not snapshot_current("Hoshivel", fabricated, "source", "cdp-windows")
            Path(os.environ["GITHUB_OUTPUT"]).unlink()
            posts.clear()
            histories["windows.yml"].clear()
            histories["cdp-windows.yml"].clear()

            # A result from the other route, even in this workflow's history,
            # must not suppress the intended work. Titles must match exactly.
            histories["windows.yml"] = [{"display_title": title("ShatteredRealms", a, "cdp-windows")}]
            histories["cdp-windows.yml"] = [
                {"display_title": title("ShatteredRealms", a)},
                {"display_title": title("ShatteredRealms", a, "cdp-windows") + "\n"},
            ]
            assert seen_runs("Hoshivel/hoshi-ci", "own") == set()
            assert seen_runs("Hoshivel/hoshi-ci", "own", "cdp-windows") == set()
            histories["windows.yml"] = [{"display_title": title("ShatteredRealms", a), "conclusion": "failure"}]
            assert seen_runs("Hoshivel/hoshi-ci", "own") == {("ShatteredRealms", a)}
            assert seen_runs("Hoshivel/hoshi-ci", "own", "cdp-windows") == set()
            histories["windows.yml"].clear()
            histories["cdp-windows.yml"].clear()

            # One aggregate budget admits one pending job from each route;
            # duplicates within one route do not consume another slot.
            fixtures[("ShatteredRealms", 10)] = fixture_pr("ShatteredRealms", 10, a, c)
            assert dispatch() == 0
            assert [workflow for workflow, _ in posts] == ["windows.yml", "cdp-windows.yml"]
            assert all(body == {"ref": "main", "inputs": sr_frozen.inputs()} for _, body in posts)
            assert "selected=2 deferred=3" in output.getvalue()
            assert len(posts) == MAX_DISPATCH == 2
            assert dispatch() == 0
            assert len(posts) == 4
            assert [workflow for workflow, _ in posts[2:]] == ["windows.yml", "cdp-windows.yml"]
            assert all(body["inputs"]["sha"] == d for _, body in posts[2:])
            assert dispatch() == 0
            assert len(posts) == 5 and posts[-1][1]["inputs"]["repo"] == "hoshi-data"
            assert dispatch() == 0 and len(posts) == 5
            identities = [(workflow, body["inputs"]["repo"], body["inputs"]["sha"]) for workflow, body in posts]
            assert len(set(identities)) == len(identities)

            # Before pilot enablement the new workflow may not exist on main.
            # Disabled profiles must not require its history to be readable.
            settings = json.loads(profile_path.read_text(encoding="utf-8"))
            settings["enabled_cdp_windows"] = []
            profile_path.write_text(json.dumps(settings), encoding="utf-8")
            workflow_reads.clear()
            assert dispatch() == 0 and len(posts) == 5
            assert workflow_reads == ["windows.yml"]

            # A named CDP pilot uses only its own workflow and the same exact
            # PR/head/base/merge identity later rechecked by worker admission.
            histories["cdp-windows.yml"].clear()
            posts.clear()
            os.environ.update(MANUAL_REPO="ShatteredRealms", MANUAL_PR="7", MANUAL_PROFILE="cdp-windows")
            assert dispatch() == 0
            assert posts == [("cdp-windows.yml", {"ref": "main", "inputs": sr_frozen.inputs()})]
            assert main(["admit", "--profile", "cdp-windows", "--repo", "ShatteredRealms",
                         "--sha", a, "--base", b, "--merge", c, "--pr", "7"]) == 0
            item = fixtures[("ShatteredRealms", 7)]
            for key, value in (("draft", True), ("state", "closed"), ("merge_commit_sha", "2" * 40)):
                previous = item[key]
                item[key] = value
                assert main(["admit", "--profile", "cdp-windows", "--repo", "ShatteredRealms",
                             "--sha", a, "--base", b, "--merge", c, "--pr", "7"]) == 1
                item[key] = previous
            item["head"]["sha"] = d
            assert admit("ShatteredRealms", a, b, c, "7", "cdp-windows") == 1
            item["head"]["sha"] = a
            assert Path(os.environ["GITHUB_OUTPUT"]).read_text() == (
                "allowed=true\nallowed=false\nallowed=false\nallowed=false\nallowed=false\n")

            histories["cdp-windows.yml"].clear()
            os.environ["DRY_RUN"] = "true"
            assert dispatch() == 0 and len(posts) == 1
            assert "Selected cdp-windows" in output.getvalue()
            reads.clear()
            os.environ.update(MANUAL_REPO="hoshi-data", DRY_RUN="false")
            assert main([]) == 1 and not reads and len(posts) == 1
            assert main(["admit", "--profile", "cdp-windows", "--repo", "hoshi-data",
                         "--sha", a, "--base", b, "--merge", c, "--pr", "7"]) == 1
            assert not reads
            os.environ.update(MANUAL_REPO="ShatteredRealms", MANUAL_PROFILE="unknown")
            assert main([]) == 1 and not reads and len(posts) == 1
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
        command.add_argument("--profile", choices=PROFILES, default="windows")
        for argument in ("repo", "sha", "base", "merge", "pr"):
            command.add_argument(f"--{argument}", required=True)
    sub.add_parser("selftest")
    args = parser.parse_args(argv)

    if args.mode == "selftest":
        return selftest()
    if args.mode == "check":
        reason = check(args.repo, args.sha, args.base, args.merge, args.pr, profile=args.profile)
        if reason:
            print(f"::error::{reason}")
            return 1
        return 0
    try:
        if args.mode == "admit":
            return admit(args.repo, args.sha, args.base, args.merge, args.pr, args.profile)
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
