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
   不用快取、不上傳 artifact，`setup-go` 明寫 `cache: false`，也不以 `actions/checkout`
   取別的倉庫——它會把 commit 標題（`HEAD is now at …`）印進日誌。
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
            if action == "actions/setup-node":
                options = step.get("with") or {}
                if "cache" in options or options.get("package-manager-cache") is not False:
                    problems.append(f"{name}：拿得到 {TOKEN}，setup-node 必須關閉套件快取")
            if action == "actions/setup-python" and "cache" in (step.get("with") or {}):
                problems.append(f"{name}：拿得到 {TOKEN}，setup-python 不得啟用套件快取")
            if action == "actions/checkout" and {"repository", "token"} & set(step.get("with") or {}):
                problems.append(f"{name}：以 actions/checkout 取別的倉庫，commit 標題會進公開日誌")
    if name == "guides-windows.yml":
        problems += check_guides_worker(doc)
    return problems


def check_guides_worker(doc: dict) -> list[str]:
    """Keep the named manual worker's provenance and sparse source boundary."""
    problems = []
    jobs = doc.get("jobs") or {}
    if events(doc) != {"workflow_dispatch"} or set(jobs) != {"eligibility", "verify"}:
        return ["guides-windows.yml：只接受具名手動工作與兩層來源核對"]
    eligibility = jobs["eligibility"]
    verify = jobs["verify"]
    if not isinstance(eligibility, dict) or not isinstance(verify, dict):
        return ["guides-windows.yml：無效工作"]
    for scope in (doc, eligibility, verify):
        if any(f"secrets.{TOKEN}" in value for value in strings(scope.get("env") or {})):
            problems.append("guides-windows.yml：私有憑證只可存在來源核對與取得來源的步驟環境")
    if (verify.get("runs-on") != "windows-latest" or verify.get("needs") != "eligibility"
            or verify.get("if") != "needs.eligibility.outputs.allowed == 'true'"):
        problems.append("guides-windows.yml：Windows 工作必須依賴來源核對成功")
    steps = verify.get("steps") or []
    eligibility_steps = eligibility.get("steps") or []
    all_steps = eligibility_steps + steps
    if len(eligibility_steps) != 2 or len(steps) != 8 or any(not isinstance(step, dict) for step in all_steps):
        return problems + ["guides-windows.yml：不接受額外或遺漏步驟"]
    admissions = [step for step in all_steps if isinstance(step, dict)
                  and "tools/dispatch.py admit" in str(step.get("run", ""))]
    if len(admissions) != 2 or any("--profile guides-windows" not in step["run"]
                                  or (step.get("env") or {}).get("SOURCE_TOKEN") != "${{ secrets.HOSHIVEL_CI_TOKEN }}"
                                  for step in admissions):
        problems.append("guides-windows.yml：兩層來源核對必須綁定具名 profile 與唯讀憑證")
    if (eligibility.get("runs-on") != "ubuntu-latest" or eligibility_steps[1] not in admissions
            or eligibility_steps[1].get("id") != "check" or steps[2] not in admissions
            or steps[2].get("id") != "current"):
        problems.append("guides-windows.yml：Linux 準入與 Windows 取得來源前核對必須各執行一次")
    current_guard = "steps.current.outputs.allowed == 'true'"
    if any(step.get("if") != current_guard for step in steps[3:]):
        problems.append("guides-windows.yml：取得來源與執行必須依賴 Windows 當次核對成功")
    python_steps = [(index, step) for index, step in enumerate(steps)
                    if isinstance(step, dict) and str(step.get("uses", "")).startswith("actions/setup-python@")]
    installers = [index for index, step in enumerate(steps)
                  if isinstance(step, dict) and step.get("run") == "python -m pip install --disable-pip-version-check --quiet pyyaml==6.0.3"]
    planners = [index for index, step in enumerate(steps)
                if isinstance(step, dict) and step.get("run") == 'python ci/tools/run-job.py plan --profile guides-windows --src src --output "${GITHUB_OUTPUT}"']
    runners = [index for index, step in enumerate(steps)
               if isinstance(step, dict) and step.get("run") == "python ci/tools/run-job.py run --profile guides-windows --src src"]
    if (len(python_steps) != 1 or python_steps[0][1].get("uses") != "actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0"
            or python_steps[0][1].get("with") != {"python-version": "3.12"}
            or len(installers) != 1 or len(planners) != 1 or len(runners) != 1
            or not python_steps[0][0] < installers[0] < planners[0] < runners[0]):
        problems.append("guides-windows.yml：固定 Python 必須先於相依安裝、計畫與執行")
    fetches = [step for step in steps if isinstance(step, dict) and "git init -q src" in str(step.get("run", ""))]
    sparse = "printf '/.github/workflows/windows-guides.yml\\n/tools/check-agent-rules.py\\n/tools/_console.py\\n' > src/.git/info/sparse-checkout"
    if (len(fetches) != 1 or fetches[0] != steps[3]
            or (fetches[0].get("env") or {}).get("TOKEN") != "${{ secrets.HOSHIVEL_CI_TOKEN }}"
            or not all(snippet in fetches[0]["run"] for snippet in (
                "git -C src config core.sparseCheckout true", "git -C src config core.sparseCheckoutCone false",
                "git -C src config remote.origin.promisor true", "git -C src config remote.origin.partialclonefilter blob:none",
                sparse, "fetch -q --filter=blob:none --depth=1 --no-tags", 'checkout -q FETCH_HEAD > "${RUNNER_TEMP}/source-checkout.log" 2>&1'))):
        problems.append("guides-windows.yml：來源只可在憑證步驟以固定 sparse 清單取得")
    for step in all_steps:
        if not isinstance(step, dict):
            continue
        if any(f"secrets.{TOKEN}" in value for value in strings(step)) and step not in admissions + fetches:
            problems.append("guides-windows.yml：來源執行不得持有私有憑證")
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
            {"uses": f"actions/checkout@{sha}", "with": {"path": "ci", "persist-credentials": False}},
            {"run": "git fetch -q", "env": {"TOKEN": "${{ secrets.HOSHIVEL_CI_TOKEN }}"}},
            {"uses": f"actions/setup-go@{sha}", "with": {"cache": False}},
        ]}},
    }
    assert check_doc("good", good) == [], check_doc("good", good)
    node = {"uses": f"actions/setup-node@{sha}", "with": {"node-version": "22", "package-manager-cache": False}}
    good["jobs"]["j"]["steps"].append(node)
    good["jobs"]["j"]["steps"].append({"uses": f"actions/setup-python@{sha}", "with": {"python-version": "3.12"}})
    assert check_doc("node without cache", good) == []
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
        "setup-go 快取": lambda d: d["jobs"]["j"]["steps"][2].update({"with": {}}),
        "setup-node 自動快取": lambda d: d["jobs"]["j"]["steps"][-2]["with"].pop("package-manager-cache"),
        "setup-node 啟用快取": lambda d: d["jobs"]["j"]["steps"][-2]["with"].update({"cache": "npm"}),
        "setup-node 開啟自動快取": lambda d: d["jobs"]["j"]["steps"][-2]["with"].update({"package-manager-cache": True}),
        "setup-python 快取": lambda d: d["jobs"]["j"]["steps"][-1]["with"].update({"cache": "pip"}),
        "setup-python 空快取宣告": lambda d: d["jobs"]["j"]["steps"][-1]["with"].update({"cache": ""}),
        "checkout 別的倉庫": lambda d: d["jobs"]["j"]["steps"][0]["with"].update({"repository": "o/private"}),
        "別的 secret": lambda d: d["jobs"]["j"].update({"env": {"X": "${{ secrets.DEPLOY_KEY }}"}}),
        "動態 secret": lambda d: d["jobs"]["j"].update({"env": {"X": "${{ toJSON(secrets) }}"}}),
        "id-token": lambda d: d["jobs"]["j"].update({"permissions": {"id-token": "write"}}),
        "environment": lambda d: d["jobs"]["j"].update({"environment": "prod"}),
        "secrets inherit": lambda d: d["jobs"].update({"k": {"uses": "./x.yml", "secrets": "inherit"}}),
        "未釘版的 reusable workflow": lambda d: d["jobs"].update({"k": {"uses": "o/r/.github/workflows/x.yml@main"}}),
    }
    for what, mutate in rejected.items():
        assert check_doc(what, variant(mutate)), f"應該拒絕：{what}"

    import copy
    guides = yaml.safe_load((ROOT / ".github/workflows/guides-windows.yml").read_text(encoding="utf-8"))
    assert check_doc("guides-windows.yml", guides) == []
    for label, mutate in {
        "missing admission token": lambda d: d["jobs"]["eligibility"]["steps"][1]["env"].pop("SOURCE_TOKEN"),
        "missing fetch token": lambda d: d["jobs"]["verify"]["steps"][3]["env"].pop("TOKEN"),
        "wrong source profile": lambda d: d["jobs"]["verify"]["steps"][2].update(run="python ci/tools/dispatch.py admit --profile windows"),
        "wrong Python revision": lambda d: d["jobs"]["verify"]["steps"][4].update(uses=f"actions/setup-python@{sha}"),
        "Python cache": lambda d: d["jobs"]["verify"]["steps"][4]["with"].update(cache="pip"),
        "source receives token": lambda d: d["jobs"]["verify"]["steps"][-1].update(env={"TOKEN": "${{ secrets.HOSHIVEL_CI_TOKEN }}"}),
        "full source checkout": lambda d: d["jobs"]["verify"]["steps"][3].update(run="git init -q src\ngit fetch -q --depth=1"),
        "automatic trigger": lambda d: d[True].update(schedule=[{"cron": "1 * * * *"}]),
        "workflow credential environment": lambda d: d.update(env={"TOKEN": "${{ secrets.HOSHIVEL_CI_TOKEN }}"}),
        "job credential environment": lambda d: d["jobs"]["verify"].update(env={"TOKEN": "${{ secrets.HOSHIVEL_CI_TOKEN }}"}),
        "Linux admission runner": lambda d: d["jobs"]["eligibility"].update({"runs-on": "windows-latest"}),
        "Windows current check after fetch": lambda d: d["jobs"]["verify"]["steps"].__setitem__(slice(2, 4), d["jobs"]["verify"]["steps"][2:4][::-1]),
        "unguarded source fetch": lambda d: d["jobs"]["verify"]["steps"][3].pop("if"),
        "unguarded source run": lambda d: d["jobs"]["verify"]["steps"][-1].pop("if"),
        "Python after installer": lambda d: d["jobs"]["verify"]["steps"].__setitem__(slice(4, 6), d["jobs"]["verify"]["steps"][4:6][::-1]),
    }.items():
        changed = copy.deepcopy(guides)
        mutate(changed)
        assert check_doc("guides-windows.yml", changed), label

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
