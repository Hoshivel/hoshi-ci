#!/usr/bin/env python3
"""檢查本倉庫的 workflow 守住了公開倉庫該守的東西。

本倉庫是公開的，而其中幾支拿得到讀私有倉庫的 HOSHIVEL_CI_TOKEN。README 對外的
每一句承諾都在這裡有一條對應的檢查——沒有被檢查的承諾，在它被打破的那天不會有
任何症狀：

1. 每個 `uses:` 都釘 40 字元 commit SHA（本倉庫的 `./` 除外）。
2. 引用的 secret 只有 GITHUB_TOKEN 與 HOSHIVEL_CI_TOKEN；不整批或動態取用。
3. 任何 workflow 都不掛外人觸發得了、又拿得到 secret 的事件
   （`pull_request_target`、`workflow_run`、`issue_comment`…）。
4. 拿得到 HOSHIVEL_CI_TOKEN 的 workflow 只由 `schedule` 與 `workflow_dispatch` 觸發，
   不用快取、不上傳 artifact，`setup-go` 明寫 `cache: false`。
5. 沒有 `id-token: write`、`permissions: write-all`、`environment:`、`secrets: inherit`。

**認不出來一律算不合規**：認不出來就檢查不到，而檢查不到的輸出與「檢查過，沒問題」
長得一模一樣。

用法：
  check-workflows.py            檢查 .github/workflows/
  check-workflows.py selftest   只跑內建案例
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "HOSHIVEL_CI_TOKEN"
ALLOWED_SECRETS = {"GITHUB_TOKEN", TOKEN}
# Events whose runs get secrets while their inputs are chosen by whoever opened
# an issue, a comment or a fork.
FORBIDDEN_EVENTS = {
    "pull_request_target", "workflow_run", "issue_comment", "issues",
    "pull_request_review", "pull_request_review_comment", "discussion", "discussion_comment",
}
TOKEN_EVENTS = {"schedule", "workflow_dispatch"}
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
SECRET_REF = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)")
DYNAMIC_SECRETS = re.compile(r"secrets\s*\[|toJSON\(\s*secrets\s*\)")


def strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from strings(key)
            yield from strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from strings(item)


def events(doc: dict) -> set[str]:
    on = doc.get("on", doc.get(True))  # YAML 1.1 reads a bare `on` as true
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return set(on)
    if isinstance(on, dict):
        return set(on)
    raise ValueError("認不出 on:")


def check_doc(name: str, doc: object) -> list[str]:
    if not isinstance(doc, dict):
        return [f"{name}：不是 mapping"]
    problems: list[str] = []
    text = list(strings(doc))

    try:
        triggers = events(doc)
    except ValueError as err:
        return [f"{name}：{err}"]
    for event in sorted(triggers & FORBIDDEN_EVENTS):
        problems.append(f"{name}：掛了 {event}")

    for value in text:
        if DYNAMIC_SECRETS.search(value):
            problems.append(f"{name}：整批或動態取用 secret")
        for secret in SECRET_REF.findall(value):
            if secret not in ALLOWED_SECRETS:
                problems.append(f"{name}：引用了 secrets.{secret}")
    uses_token = any(f"secrets.{TOKEN}" in value for value in text)
    if uses_token and not triggers <= TOKEN_EVENTS:
        problems.append(f"{name}：拿得到 {TOKEN}，卻由 {', '.join(sorted(triggers - TOKEN_EVENTS))} 觸發")

    for scope in [doc] + list((doc.get("jobs") or {}).values()):
        if not isinstance(scope, dict):
            problems.append(f"{name}：有 job 不是 mapping")
            continue
        permissions = scope.get("permissions")
        if permissions == "write-all" or (isinstance(permissions, dict) and permissions.get("id-token") == "write"):
            problems.append(f"{name}：拿得到 id-token: write")
        if "environment" in scope:
            problems.append(f"{name}：宣告了 environment")
        if scope.get("secrets") == "inherit":
            problems.append(f"{name}：secrets: inherit")
        uses = scope.get("uses")
        if isinstance(uses, str) and not uses.startswith("./") and not PINNED.match(uses):
            problems.append(f"{name}：{uses} 沒有釘 SHA")
        for step in scope.get("steps") or []:
            uses = step.get("uses") if isinstance(step, dict) else None
            if uses is None:
                continue
            if not uses.startswith("./") and not PINNED.match(uses):
                problems.append(f"{name}：{uses} 沒有釘 SHA")
            if not uses_token:
                continue
            action = uses.split("@", 1)[0]
            if action.startswith(("actions/cache", "actions/upload-artifact")):
                problems.append(f"{name}：拿得到 {TOKEN}，卻用了 {action}")
            if action == "actions/setup-go" and (step.get("with") or {}).get("cache") is not False:
                problems.append(f"{name}：拿得到 {TOKEN}，setup-go 卻沒有寫 cache: false")
    return problems


def check(root: Path = ROOT) -> list[str]:
    paths = sorted((root / ".github" / "workflows").glob("*.y*ml"))
    if not paths:
        return ["找不到任何 workflow"]
    problems: list[str] = []
    for path in paths:
        problems += check_doc(path.name, yaml.safe_load(path.read_text(encoding="utf-8")))
    return problems


def selftest() -> int:
    sha = "0" * 40
    good = {
        "on": {"workflow_dispatch": None},
        "permissions": {"contents": "read"},
        "jobs": {"j": {"runs-on": "windows-latest", "steps": [
            {"uses": f"actions/checkout@{sha}", "with": {"token": "${{ secrets.HOSHIVEL_CI_TOKEN }}"}},
            {"uses": f"actions/setup-go@{sha}", "with": {"cache": False}},
        ]}},
    }
    assert check_doc("good", good) == [], check_doc("good", good)
    assert check_doc("bare on", yaml.safe_load("on: push\njobs: {}\n")) == []

    def variant(mutate) -> dict:
        import copy
        doc = copy.deepcopy(good)
        mutate(doc)
        return doc

    rejected = {
        "PR 觸發": lambda d: d["on"].update({"pull_request": None}),
        "pull_request_target": lambda d: d.update({"on": {"pull_request_target": None}}),
        "tag 釘版": lambda d: d["jobs"]["j"]["steps"].append({"uses": "actions/cache@v4"}),
        "快取": lambda d: d["jobs"]["j"]["steps"].append({"uses": f"actions/cache/restore@{sha}"}),
        "artifact": lambda d: d["jobs"]["j"]["steps"].append({"uses": f"actions/upload-artifact@{sha}"}),
        "setup-go 快取": lambda d: d["jobs"]["j"]["steps"][1].update({"with": {}}),
        "別的 secret": lambda d: d["jobs"]["j"].update({"env": {"X": "${{ secrets.DEPLOY_KEY }}"}}),
        "動態 secret": lambda d: d["jobs"]["j"].update({"env": {"X": "${{ toJSON(secrets) }}"}}),
        "id-token": lambda d: d["jobs"]["j"].update({"permissions": {"id-token": "write"}}),
        "environment": lambda d: d["jobs"]["j"].update({"environment": "prod"}),
        "secrets inherit": lambda d: d["jobs"].update({"k": {"uses": "./x.yml", "secrets": "inherit"}}),
        "未釘版的 reusable workflow": lambda d: d["jobs"].update({"k": {"uses": "o/r/.github/workflows/x.yml@main"}}),
    }
    for what, mutate in rejected.items():
        assert check_doc(what, variant(mutate)), f"應該拒絕：{what}"

    with tempfile.TemporaryDirectory() as tmp:
        assert check(Path(tmp)) == ["找不到任何 workflow"]

    print("check-workflows.py selftest：全部通過")
    return 0


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if argv[1:] == ["selftest"]:
        return selftest()
    if argv[1:]:
        print(__doc__)
        return 2
    problems = check()
    for problem in problems:
        print(f"不合規  {problem}")
    if problems:
        print(f"\n{len(problems)} 個問題。")
        return 1
    print("workflow 全部合規。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
