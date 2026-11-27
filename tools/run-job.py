#!/usr/bin/env python3
"""照被驗證倉庫自己的 job 定義，在這臺 runner 上跑它的 run 步驟。

hoshi-ci 不保存被驗證倉庫的指令。它讀 checkout 下來那個 commit 的 workflow 檔，
取出指定的 job，照順序執行其中的 `run` 步驟——所以驗證內容只寫在私有倉庫那一份：
那邊改了，這裡跑的就跟著改，沒有第二份會漂移。

只認得具名 profile 實際用到的形狀。不認得的鍵、`${{ }}` 算式、profile 未允許的
action 一律是錯誤，不是跳過：一個安靜沒跑的步驟，與一個跑過且通過的步驟長得一模一樣。

唯一的例外是私有 module 憑證那一步：它由 workflow 自己的下載步驟代替，而那是唯一
看得到 token 的步驟。被驗證的程式碼開始執行時，token 已經不在了。

輸出被收下而不是直接印出。本倉庫是公開的，日誌僅含步驟序號、成敗、結束碼與時間。
步驟名稱、命令及完整輸出留在 runner 上，要看就在開發機以同一個 commit 重跑。

用法：
  run-job.py plan --src DIR [--output FILE]
                              驗證 job 形狀，把 go-version-file 與 module-dir
                              以 key=value 附加到 FILE，stdout 只報形狀通過
  run-job.py run  --src DIR   執行 run 步驟
  run-job.py plan --src DIR --profile cdp-windows
                              讀固定 CDP job，輸出 node-version
  run-job.py selftest         只跑內建案例
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

WORKFLOW = ".github/workflows/ci.yml"
JOB = "verify-windows"
RUNS_ON = "windows-latest"
PROFILES = {
    "windows": (WORKFLOW, JOB),
    "cdp-windows": (".github/workflows/cdp-lifecycle.yml", "lifecycle"),
}
CDP_CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
CDP_SETUP_NODE = "actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1"
CDP_ACTIONS = frozenset((CDP_CHECKOUT, CDP_SETUP_NODE))

JOB_KEYS = {"if", "name", "needs", "runs-on", "timeout-minutes", "env", "defaults", "steps"}
STEP_KEYS = {"name", "id", "run", "uses", "with", "env", "shell", "working-directory"}
SHELLS = {"bash", "pwsh"}
# The shell GitHub uses on Windows when neither the step nor `defaults` names one.
DEFAULT_SHELL = "pwsh"
# GitHub's own job timeout when `timeout-minutes` is absent.
DEFAULT_TIMEOUT = 360

CREDENTIAL = re.compile(r"\$\{\{\s*secrets\.HOSHIVEL_CI_TOKEN\s*\}\}")

# File commands the runner reads after a step. A child process must not be
# able to write this job's summary, outputs, env or path, so each gets a
# throwaway file instead.
FILE_COMMANDS = ("GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH", "GITHUB_STATE", "GITHUB_STEP_SUMMARY")


class JobError(Exception):
    """The job uses a shape this runner does not understand."""


@dataclass
class Step:
    label: str
    shell: str
    script: str
    env: dict[str, str]
    cwd: str


@dataclass
class Plan:
    go_version_file: str | None
    timeout_minutes: int
    env: dict[str, str]
    steps: list[Step]
    notes: list[str] = field(default_factory=list)
    node_version: str | None = None

    @property
    def module_dir(self) -> str:
        if self.go_version_file is None:
            raise JobError("this profile has no Go module")
        parent = PurePosixPath(self.go_version_file).parent
        return str(parent) if str(parent) else "."


def literal(where: str, value: object) -> None:
    if isinstance(value, str) and "${{" in value:
        raise JobError(f"{where} 含有 ${{{{ }}}} 算式，這裡不求值")


def env_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def relative(where: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise JobError(f"{where} 必須是非空字串")
    literal(where, value)
    if not re.fullmatch(r"[A-Za-z0-9._/\\-]+", value):
        raise JobError("relative path contains unsupported characters")
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", value):
        raise JobError(f"{where} 必須是倉庫內的相對路徑：{value!r}")
    return str(path)


def profile_source(profile: str, workflow: str | None = None,
                   job: str | None = None) -> tuple[str, str]:
    if profile not in PROFILES:
        raise JobError("unsupported execution profile")
    expected_workflow, expected_job = PROFILES[profile]
    if workflow not in (None, expected_workflow) or job not in (None, expected_job):
        raise JobError("workflow or job does not match execution profile")
    return expected_workflow, expected_job


def build_plan(doc: dict, job_name: str = JOB, profile: str = "windows") -> Plan:
    profile_source(profile, job=job_name)
    if not isinstance(doc, dict):
        raise JobError("workflow 不是一個 mapping")
    job = (doc.get("jobs") or {}).get(job_name)
    if not isinstance(job, dict):
        raise JobError(f"沒有 job `{job_name}`")

    unknown = set(job) - JOB_KEYS
    if unknown:
        raise JobError(f"job 有不認得的鍵：{', '.join(sorted(unknown))}")
    # The dispatcher and worker admission independently bind an open Ready PR
    # to this source. This dependency supplies no build outputs or services.
    # No other prerequisite can be silently omitted by the portable runner.
    if "needs" in job and job["needs"] != ["eligibility"]:
        raise JobError("unsupported prerequisite job")
    if job.get("runs-on") != RUNS_ON:
        raise JobError(f"runs-on 是 {job.get('runs-on')!r}，這裡只跑 {RUNS_ON}")
    timeout = job.get("timeout-minutes", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise JobError(f"timeout-minutes 必須是正整數：{timeout!r}")

    env: dict[str, str] = {}
    for scope, mapping in (("workflow env", doc.get("env")), ("job env", job.get("env"))):
        if mapping is None:
            continue
        if not isinstance(mapping, dict):
            raise JobError(f"{scope} 不是 mapping")
        for key, value in mapping.items():
            literal(f"{scope} {key}", value)
            env[str(key)] = env_value(value)

    defaults: dict[str, str] = {}
    for scope, block in (("workflow defaults", doc.get("defaults")), ("job defaults", job.get("defaults"))):
        if block is None:
            continue
        if not isinstance(block, dict) or set(block) - {"run"}:
            raise JobError(f"{scope} 只認 run")
        run_defaults = block.get("run") or {}
        if not isinstance(run_defaults, dict) or set(run_defaults) - {"shell", "working-directory"}:
            raise JobError(f"{scope}.run 只認 shell 與 working-directory")
        for key, value in run_defaults.items():
            literal(f"{scope}.run.{key}", value)
            defaults[key] = value

    steps: list[Step] = []
    notes: list[str] = []
    go_version_file: str | None = None
    node_version: str | None = None
    checkout_seen = False

    for index, step in enumerate(job.get("steps") or [], 1):
        where = f"第 {index} 步"
        if not isinstance(step, dict):
            raise JobError(f"{where} 不是 mapping")
        unknown = set(step) - STEP_KEYS
        if unknown:
            raise JobError(f"{where} 有不認得的鍵：{', '.join(sorted(unknown))}")

        if "uses" in step:
            if "run" in step:
                raise JobError(f"{where} 同時有 uses 與 run")
            if profile == "cdp-windows" and (
                    not isinstance(step["uses"], str)
                    or step["uses"] not in CDP_ACTIONS):
                raise JobError("CDP action revision is not supported")
            action = str(step["uses"]).split("@", 1)[0]
            options = step.get("with") or {}
            if not isinstance(options, dict):
                raise JobError("action options must be a mapping")
            if profile == "cdp-windows" and set(step) - {"name", "uses", "with"}:
                raise JobError("unsupported CDP action fields")
            if action == "actions/checkout":
                # The workflow has already checked out this commit; options
                # would mean a different checkout, which is not replicated.
                if options:
                    raise JobError(f"{where}：actions/checkout 帶了選項，這裡不重現")
                if profile == "cdp-windows" and (checkout_seen or node_version is not None or steps):
                    raise JobError("CDP checkout must occur once before setup and execution")
                checkout_seen = True
                continue
            if action == "actions/setup-go":
                if profile != "windows":
                    raise JobError("Go setup is not allowed by this profile")
                if go_version_file is not None:
                    raise JobError(f"{where}：第二個 actions/setup-go")
                if steps:
                    raise JobError(f"{where}：actions/setup-go 在 run 步驟之後")
                if set(options) - {"go-version-file", "cache"}:
                    raise JobError(f"{where}：actions/setup-go 只認 go-version-file")
                go_version_file = relative(f"{where} go-version-file", options.get("go-version-file"))
                continue
            if action == "actions/setup-node":
                if profile != "cdp-windows":
                    raise JobError("Node setup is not allowed by this profile")
                if node_version is not None or steps or not checkout_seen:
                    raise JobError("CDP Node setup must occur once after checkout and before execution")
                if options != {"node-version": "22"}:
                    raise JobError("CDP requires Node 22 without cache or other setup options")
                node_version = "22"
                continue
            raise JobError("action is not supported by this profile")

        run = step.get("run")
        if not isinstance(run, str) or not run.strip():
            raise JobError(f"{where} 沒有 run")
        step_env = step.get("env") or {}
        if not isinstance(step_env, dict):
            raise JobError(f"{where} 的 env 不是 mapping")

        if any(isinstance(v, str) and CREDENTIAL.search(v) for v in step_env.values()):
            if profile != "windows":
                raise JobError("credential steps cannot be omitted by this profile")
            notes.append("Module authentication is handled before source execution.")
            continue

        literal(f"{where} run", run)
        literal(f"{where} name", step.get("name"))
        for key, value in step_env.items():
            literal(f"{where} env {key}", value)

        shell = step.get("shell", defaults.get("shell", DEFAULT_SHELL))
        if shell not in SHELLS:
            raise JobError(f"{where} 的 shell 是 {shell!r}；這裡只認 {', '.join(sorted(SHELLS))}")
        cwd = relative(f"{where} working-directory",
                       step.get("working-directory", defaults.get("working-directory", ".")))

        steps.append(Step(
            label=f"Step {len(steps) + 1}",
            shell=shell,
            script=run,
            env={str(k): env_value(v) for k, v in step_env.items()},
            cwd=cwd,
        ))

    if profile == "windows" and go_version_file is None:
        raise JobError("沒有 actions/setup-go 的 go-version-file")
    if profile == "cdp-windows" and node_version is None:
        raise JobError("CDP requires Node setup")
    if not steps:
        raise JobError("沒有任何 run 步驟")
    return Plan(go_version_file, timeout, env, steps, notes, node_version)


def load_plan(src: Path, workflow: str | None = None, job: str | None = None,
              profile: str = "windows") -> Plan:
    import yaml  # installed, pinned, by the workflow step before this one

    workflow, job = profile_source(profile, workflow, job)
    path = src / relative("workflow", workflow)
    if not path.is_file() or not path.resolve().is_relative_to(src.resolve()):
        raise JobError(f"{workflow} 不存在")
    plan = build_plan(yaml.safe_load(path.read_text(encoding="utf-8")), job, profile)
    if plan.go_version_file is not None:
        module = src / plan.go_version_file
        if not module.is_file() or not module.resolve().is_relative_to(src.resolve()):
            raise JobError("module file escapes or is absent from source")
    return plan


def git_bash() -> str:
    # GitHub runs `shell: bash` on Windows with Git Bash, not the WSL
    # launcher that also answers to `bash` in System32.
    if os.name != "nt":
        return shutil.which("bash") or "bash"
    git = shutil.which("git")
    candidates = [Path(git).resolve().parent.parent / "bin" / "bash.exe"] if git else []
    candidates.append(Path(r"C:\Program Files\Git\bin\bash.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise JobError("找不到 Git Bash")


def pwsh() -> str:
    found = shutil.which("pwsh")
    if not found:
        raise JobError("找不到 pwsh")
    return found


def pwsh_script(script: str) -> str:
    # The wrapper GitHub puts around a pwsh step, so a native command failing
    # in the middle of a multi-line script behaves exactly as it does there.
    return ("$ErrorActionPreference = 'stop'\n" + script.rstrip("\n") +
            "\nif ((Test-Path -LiteralPath variable:\\LASTEXITCODE)) { exit $LASTEXITCODE }\n")


def command(step: Step, scratch: Path, index: int) -> list[str]:
    if step.shell == "bash":
        path = scratch / f"step{index}.sh"
        path.write_bytes(step.script.encode("utf-8"))
        return [git_bash(), "--noprofile", "--norc", "-eo", "pipefail", path.as_posix()]
    path = scratch / f"step{index}.ps1"
    path.write_bytes(b"\xef\xbb\xbf" + pwsh_script(step.script).encode("utf-8"))
    return [pwsh(), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", f". '{path}'"]


def child_env(base: dict[str, str], plan: Plan, step: Step, src: Path, scratch: Path) -> dict[str, str]:
    env = dict(base)
    env.update(plan.env)
    env.update(step.env)
    for name in tuple(env):
        upper = name.upper()
        if (upper in {"TOKEN", "SECRET", "PASSWORD", "GIT_ASKPASS", "SSH_ASKPASS", "SSH_AUTH_SOCK"}
                or upper.endswith(("_TOKEN", "_SECRET", "_PASSWORD", "_KEY"))
                or upper.startswith(("ACTIONS_", "GIT_CONFIG_"))):
            del env[name]
    env["GITHUB_WORKSPACE"] = str(src.resolve())
    for name in FILE_COMMANDS:
        env[name] = str(scratch / f"discard-{name.lower()}")
    return env


def kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=30, check=False)
        if result.returncode and proc.poll() is None:
            raise JobError("process tree termination failed")
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait(timeout=30)


def execute(plan: Plan, src: Path) -> int:
    scratch = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "hoshi-ci-job"
    scratch.mkdir(parents=True, exist_ok=True)

    deadline = time.monotonic() + plan.timeout_minutes * 60
    base = dict(os.environ)
    rows: list[tuple[str, str, str]] = []
    failed = False

    for index, step in enumerate(plan.steps, 1):
        label = f"Step {index}"
        if failed:
            rows.append((label, "Not run", ""))
            print(f"{label}: not run")
            continue

        cwd = src / step.cwd
        log = scratch / f"step{index}.log"
        start = time.monotonic()
        code: int | None = None
        reason = ""
        if not cwd.is_dir() or not cwd.resolve().is_relative_to(src.resolve()):
            reason = "working directory unavailable"
        elif deadline - start <= 0:
            reason = "timeout"
        else:
            with log.open("wb") as out:
                proc = subprocess.Popen(
                    command(step, scratch, index),
                    cwd=cwd,
                    env=child_env(base, plan, step, src, scratch),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name != "nt",
                )
                try:
                    code = proc.wait(timeout=deadline - start)
                except subprocess.TimeoutExpired:
                    kill_tree(proc)
                    reason = "timeout"
        elapsed = f"{time.monotonic() - start:.0f}s"

        if code == 0:
            rows.append((label, "Success", elapsed))
            print(f"{label}: success ({elapsed})")
            continue

        failed = True
        reason = reason or f"exit {code}"
        rows.append((label, reason, elapsed))
        print(f"{label}: failure ({elapsed}, {reason})")
        print(f"::error::{label} failed")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as out:
            label = "CDP Windows" if plan.node_version else JOB
            out.write(f"### {label}\n\n| 步驟 | 結果 | 時間 |\n|---|---|---|\n")
            for label, result, elapsed in rows:
                out.write(f"| `{label}` | {result} | {elapsed} |\n")
            out.write("\nDetailed output is withheld. Reproduce this source revision privately.\n")
    return 1 if failed else 0


def selftest() -> int:
    base = {
        "jobs": {
            JOB: {
                "if": "github.event_name == 'workflow_dispatch'",
                "name": "Windows",
                "runs-on": RUNS_ON,
                "timeout-minutes": 25,
                "env": {"GOPRIVATE": "github.com/hoshivel/*"},
                "defaults": {"run": {"working-directory": "backend"}},
                "steps": [
                    {"uses": "actions/checkout@" + "0" * 40},
                    {"uses": "actions/setup-go@" + "0" * 40, "with": {"go-version-file": "backend/go.mod"}},
                    {"name": "私有 module 憑證", "shell": "bash",
                     "env": {"TOKEN": "${{ secrets.HOSHIVEL_CI_TOKEN }}", "IS_DEPENDABOT": "${{ github.actor }}"},
                     "run": "git config --global ..."},
                    {"run": "go build ./..."},
                    {"name": "cross", "shell": "bash", "env": {"GOOS": "linux", "CGO_ENABLED": 0},
                     "run": "set -euo pipefail\ngo build ./...\n"},
                ],
            }
        }
    }

    def variant(mutate) -> dict:
        import copy
        doc = copy.deepcopy(base)
        mutate(doc["jobs"][JOB])
        return doc

    plan = build_plan(base)
    assert plan.go_version_file == "backend/go.mod", plan.go_version_file
    assert plan.module_dir == "backend", plan.module_dir
    assert plan.timeout_minutes == 25
    assert plan.env == {"GOPRIVATE": "github.com/hoshivel/*"}
    assert [s.label for s in plan.steps] == ["Step 1", "Step 2"], plan.steps
    assert [s.shell for s in plan.steps] == ["pwsh", "bash"]
    assert [s.cwd for s in plan.steps] == ["backend", "backend"]
    assert plan.steps[1].env == {"GOOS": "linux", "CGO_ENABLED": "0"}
    assert len(plan.notes) == 1
    admitted = variant(lambda j: j.update({"needs": ["eligibility"]}))
    assert build_plan(admitted).steps == plan.steps

    root = variant(lambda j: j["steps"][1]["with"].update({"go-version-file": "go.mod"}))
    assert build_plan(root).module_dir == "."

    rejected = {
        "未知的 job 鍵": lambda j: j.update({"services": {}}),
        "未知的步驟鍵": lambda j: j["steps"][3].update({"continue-on-error": True}),
        "run 裡的算式": lambda j: j["steps"][3].update({"run": "go test ${{ matrix.pkg }}"}),
        "env 裡的算式": lambda j: j["env"].update({"X": "${{ vars.X }}"}),
        "別的 action": lambda j: j["steps"].insert(2, {"uses": "actions/cache@" + "0" * 40}),
        "checkout 帶選項": lambda j: j["steps"][0].update({"with": {"fetch-depth": 0}}),
        "別的 runner": lambda j: j.update({"runs-on": "windows-2025"}),
        "別的 shell": lambda j: j["steps"][3].update({"shell": "cmd"}),
        "跳出倉庫的路徑": lambda j: j["steps"][3].update({"working-directory": "../x"}),
        "絕對路徑": lambda j: j["steps"][3].update({"working-directory": "C:/x"}),
        "沒有 setup-go": lambda j: j["steps"].pop(1),
        "setup-go 在 run 之後": lambda j: j["steps"].append(j["steps"].pop(1)),
        "只剩憑證那一步": lambda j: j.update({"steps": j["steps"][:3]}),
        "timeout 不是整數": lambda j: j.update({"timeout-minutes": "25"}),
        "其他前置工作": lambda j: j.update({"needs": ["build"]}),
        "混合前置工作": lambda j: j.update({"needs": ["eligibility", "build"]}),
        "未識別的前置型態": lambda j: j.update({"needs": "eligibility"}),
        "步驟條件": lambda j: j["steps"][3].update({"if": "always()"}),
        "輸出檔注入": lambda j: j["steps"][1]["with"].update({"go-version-file": "go.mod\nextra=value"}),
    }
    for what, mutate in rejected.items():
        try:
            build_plan(variant(mutate))
        except JobError:
            continue
        raise AssertionError(f"應該拒絕：{what}")

    cdp = {
        "jobs": {"lifecycle": {
            "needs": ["eligibility"],
            "runs-on": RUNS_ON,
            "timeout-minutes": 5,
            "steps": [
                {"uses": CDP_CHECKOUT},
                {"uses": CDP_SETUP_NODE, "with": {"node-version": "22"}},
                {"run": "node --test fixture.test.mjs"},
            ],
        }},
    }
    cdp_plan = build_plan(cdp, "lifecycle", "cdp-windows")
    assert cdp_plan.node_version == "22" and cdp_plan.go_version_file is None
    assert cdp_plan.timeout_minutes == 5 and len(cdp_plan.steps) == 1
    assert cdp_plan.steps[0].shell == "pwsh" and cdp_plan.steps[0].cwd == "."
    cdp_rejected = {
        "missing setup": lambda j: j["steps"].pop(1),
        "missing checkout": lambda j: j["steps"].pop(0),
        "duplicate checkout": lambda j: j["steps"].insert(1, j["steps"][0]),
        "duplicate setup": lambda j: j["steps"].insert(2, j["steps"][1]),
        "late setup": lambda j: j["steps"].append(j["steps"].pop(1)),
        "other Node": lambda j: j["steps"][1]["with"].update({"node-version": "23"}),
        "numeric Node": lambda j: j["steps"][1]["with"].update({"node-version": 22}),
        "setup cache": lambda j: j["steps"][1]["with"].update({"cache": "npm"}),
        "setup environment": lambda j: j["steps"][1].update({"env": {"X": "value"}}),
        "checkout without pin": lambda j: j["steps"][0].update({"uses": "actions/checkout"}),
        "other checkout commit": lambda j: j["steps"][0].update({"uses": "actions/checkout@" + "1" * 40}),
        "other Node commit": lambda j: j["steps"][1].update({"uses": "actions/setup-node@" + "1" * 40}),
        "Node tag pin": lambda j: j["steps"][1].update({"uses": "actions/setup-node@v7"}),
        "Node variable pin": lambda j: j["steps"][1].update({"uses": "actions/setup-node@${{ vars.SHA }}"}),
        "Node missing pin": lambda j: j["steps"][1].update({"uses": "actions/setup-node@"}),
        "Go setup": lambda j: j["steps"].insert(2, base["jobs"][JOB]["steps"][1]),
        "credential omission": lambda j: j["steps"].insert(2, base["jobs"][JOB]["steps"][2]),
        "run expression": lambda j: j["steps"][2].update({"run": "node ${{ matrix.test }}"}),
        "other prerequisite": lambda j: j.update({"needs": ["eligibility", "build"]}),
    }
    import copy
    for what, mutate in cdp_rejected.items():
        changed = copy.deepcopy(cdp)
        mutate(changed["jobs"]["lifecycle"])
        try:
            build_plan(changed, "lifecycle", "cdp-windows")
        except JobError:
            continue
        raise AssertionError(f"CDP should reject: {what}")
    import yaml
    wrapper_path = Path(__file__).resolve().parent.parent / ".github/workflows/cdp-windows.yml"
    wrapper = yaml.safe_load(wrapper_path.read_text(encoding="utf-8"))
    wrapper_actions = [step["uses"] for job in wrapper["jobs"].values()
                       for step in job["steps"] if "uses" in step]
    assert [action for action in wrapper_actions if action.startswith("actions/setup-node@")] == [CDP_SETUP_NODE]
    assert all(action == CDP_CHECKOUT for action in wrapper_actions if action.startswith("actions/checkout@"))
    for arguments in (("unknown", None, None), ("windows", None, "lifecycle"),
                      ("cdp-windows", WORKFLOW, None), ("cdp-windows", None, JOB)):
        try:
            profile_source(*arguments)
        except JobError:
            continue
        raise AssertionError("profile source binding accepted an unsupported combination")
    try:
        build_plan({"jobs": {JOB: cdp["jobs"]["lifecycle"]}})
    except JobError:
        pass
    else:
        raise AssertionError("Go profile accepted Node setup")
    try:
        build_plan({"jobs": {}})
    except JobError:
        pass
    else:
        raise AssertionError("應該拒絕：沒有那個 job")

    wrapped = pwsh_script("go build ./...\ngo vet ./...\n")
    assert wrapped.startswith("$ErrorActionPreference = 'stop'\n")
    assert wrapped.endswith("{ exit $LASTEXITCODE }\n")

    scratch = Path(tempfile.gettempdir())
    ambient = {"GITHUB_STEP_SUMMARY": "real", "PATH": "p", "SOURCE_TOKEN": "private",
               "GITHUB_TOKEN": "private", "GH_TOKEN": "private", "TOKEN": "private",
               "ACTIONS_RUNTIME_TOKEN": "private", "GIT_CONFIG_KEY_0": "private",
               "SSH_AUTH_SOCK": "private", "GIT_ASKPASS": "private"}
    env = child_env(ambient, plan, plan.steps[1], Path("."), scratch)
    assert env["GITHUB_STEP_SUMMARY"] != "real"
    assert env["GOOS"] == "linux" and env["GOPRIVATE"] == "github.com/hoshivel/*" and env["PATH"] == "p"
    assert not any(value == "private" for value in env.values())
    replacement = Step("private", "pwsh", "unused", {"GITHUB_OUTPUT": "outside", "GH_TOKEN": "private"}, ".")
    env = child_env({}, plan, replacement, Path("."), scratch)
    assert env["GITHUB_OUTPUT"] != "outside" and "GH_TOKEN" not in env

    # Prefix allowlists cannot make arbitrary test output public. A child can
    # print a private value after any accepted compiler/test/annotation prefix.
    from unittest.mock import patch
    private = "PRIVATE_OUTPUT_MUST_NOT_BE_PUBLISHED"
    script = "import sys; print(" + repr("\n".join(prefix + private for prefix in (
        "::error::", "# ", "go: ", "--- FAIL: Test/", "FAIL ", "::warning::"))) + "); sys.exit(17)"
    with tempfile.TemporaryDirectory() as folder:
        work = Path(folder)
        summary = work / "summary.md"
        for module, node in (("go.mod", None), (None, "22")):
            public = io.StringIO()
            sample = Plan(module, 1, {}, [Step(private, "pwsh", private, {}, ".")], node_version=node)
            with patch.dict(os.environ, {"RUNNER_TEMP": folder, "GITHUB_STEP_SUMMARY": str(summary)}), \
                 patch.object(sys.modules[__name__], "command", return_value=[sys.executable, "-c", script]), \
                 contextlib.redirect_stdout(public):
                assert execute(sample, work) == 1
            assert private not in public.getvalue() and private not in summary.read_text(encoding="utf-8")
            assert "Step 1" in public.getvalue() and "exit 17" in public.getvalue()
            assert private in (work / "hoshi-ci-job/step1.log").read_text()

        invalid = work / ".github/workflows"
        invalid.mkdir(parents=True)
        import yaml
        (invalid / "cdp-lifecycle.yml").write_text(yaml.safe_dump(cdp), encoding="utf-8")
        output = work / "plan-output"
        public = io.StringIO()
        with contextlib.redirect_stdout(public):
            assert main(["plan", "--src", str(work), "--profile", "cdp-windows",
                         "--output", str(output)]) == 0
        assert output.read_text(encoding="utf-8") == "node-version=22\n"
        assert public.getvalue() == "Job definition accepted\n"
        public = io.StringIO()
        with contextlib.redirect_stdout(public):
            assert main(["plan", "--src", str(work), "--profile", "cdp-windows",
                         "--workflow", WORKFLOW]) == 1
        assert public.getvalue() == "::error::Unsupported job definition or unavailable source\n"
        (invalid / "ci.yml").write_text("jobs: [" + private, encoding="utf-8")
        public = io.StringIO()
        with contextlib.redirect_stdout(public):
            assert main(["plan", "--src", str(work)]) == 1
        assert private not in public.getvalue()

        # A timed-out shell can leave a test server beneath it. Exercise the
        # process-tree terminator with an owned parent and a live child writer.
        heartbeat = work / "child-heartbeat"
        child_script = ("import pathlib,time; p=pathlib.Path(" + repr(str(heartbeat)) + "); "
                        "\nwhile True:\n p.write_text(str(time.monotonic())); time.sleep(.02)")
        parent_script = ("import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," +
                         repr(child_script) + "]); time.sleep(30)")
        process = subprocess.Popen([sys.executable, "-c", parent_script],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=os.name != "nt")
        try:
            until = time.monotonic() + 5
            while not heartbeat.exists() and time.monotonic() < until:
                time.sleep(.02)
            assert heartbeat.exists(), "owned child did not start"
            kill_tree(process)
            stopped = heartbeat.read_bytes()
            time.sleep(.1)
            assert heartbeat.read_bytes() == stopped, "descendant survived process-tree termination"
        finally:
            if process.poll() is None:
                kill_tree(process)

    print("run-job.py selftest：全部通過")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("plan", "run", "selftest"))
    parser.add_argument("--src", type=Path, help="被驗證倉庫 checkout 的位置")
    parser.add_argument("--profile", choices=tuple(PROFILES), default="windows")
    parser.add_argument("--workflow", help="必須與 profile 的固定 workflow 相符")
    parser.add_argument("--job", help="必須與 profile 的固定 job 相符")
    parser.add_argument("--output", help="plan 的 key=value 附加到這個檔（$GITHUB_OUTPUT）")
    args = parser.parse_args(argv)

    if args.mode == "selftest":
        return selftest()
    if args.src is None:
        parser.error("plan 與 run 需要 --src")

    try:
        plan = load_plan(args.src, args.workflow, args.job, args.profile)
    except Exception:
        print("::error::Unsupported job definition or unavailable source")
        return 1

    if args.mode == "plan":
        if plan.node_version is not None:
            lines = f"node-version={plan.node_version}\n"
        else:
            lines = f"go-version-file={plan.go_version_file}\nmodule-dir={plan.module_dir}\n"
        if args.output:
            with open(args.output, "a", encoding="utf-8") as out:
                out.write(lines)
        print("Job definition accepted")
        return 0
    try:
        return execute(plan, args.src)
    except Exception:
        print("::error::Job execution failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
