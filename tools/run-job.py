#!/usr/bin/env python3
"""照被驗證倉庫自己的 job 定義，在這臺 runner 上跑它的 run 步驟。

hoshi-ci 不保存被驗證倉庫的指令。它讀 checkout 下來那個 commit 的 workflow 檔，
取出指定的 job，照順序執行其中的 `run` 步驟——所以驗證內容只寫在私有倉庫那一份：
那邊改了，這裡跑的就跟著改，沒有第二份會漂移。

只認得那些 job 實際用到的形狀。不認得的鍵、`${{ }}` 算式、checkout／setup-go 以外的
action 一律是錯誤，不是跳過：一個安靜沒跑的步驟，與一個跑過且通過的步驟長得一模一樣。

唯一的例外是私有 module 憑證那一步：它由 workflow 自己的下載步驟代替，而那是唯一
看得到 token 的步驟。被驗證的程式碼開始執行時，token 已經不在了。

輸出被收下而不是直接印出。本倉庫是公開的，所以日誌只印每一步的成敗，失敗的那一步
也只印點名失敗對象的行（測試、套件、工具鏈錯誤）。完整輸出留在 runner 上，要看就在
開發機以同一個 commit 重跑。

用法：
  run-job.py plan --src DIR [--output FILE]
                              驗證 job 形狀，把 go-version-file 與 module-dir
                              以 key=value 附加到 FILE（預設印在 stdout）
  run-job.py run  --src DIR   執行 run 步驟
  run-job.py selftest         只跑內建案例
"""

from __future__ import annotations

import argparse
import os
import re
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

JOB_KEYS = {"if", "name", "runs-on", "timeout-minutes", "env", "defaults", "steps"}
STEP_KEYS = {"name", "id", "run", "uses", "with", "env", "shell", "working-directory"}
SHELLS = {"bash", "pwsh"}
# The shell GitHub uses on Windows when neither the step nor `defaults` names one.
DEFAULT_SHELL = "pwsh"
# GitHub's own job timeout when `timeout-minutes` is absent.
DEFAULT_TIMEOUT = 360

CREDENTIAL = re.compile(r"\$\{\{\s*secrets\.HOSHIVEL_CI_TOKEN\s*\}\}")

# The only lines of a failing step that reach the public log: which test or
# package failed, and toolchain or workflow errors. Never source text,
# assertion messages or test output.
SHOWN = re.compile(r"^(?:\s*--- FAIL: |FAIL(?:\s|$)|# |go: |::error)")
SHOWN_LIMIT = 60

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
    go_version_file: str
    timeout_minutes: int
    env: dict[str, str]
    steps: list[Step]
    notes: list[str] = field(default_factory=list)

    @property
    def module_dir(self) -> str:
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
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", value):
        raise JobError(f"{where} 必須是倉庫內的相對路徑：{value!r}")
    return str(path)


def label_of(step: dict, run: str) -> str:
    name = step.get("name")
    if name:
        return str(name)
    first = next((line.strip() for line in run.splitlines() if line.strip()), "")
    return first[:80]


def build_plan(doc: dict, job_name: str = JOB) -> Plan:
    if not isinstance(doc, dict):
        raise JobError("workflow 不是一個 mapping")
    job = (doc.get("jobs") or {}).get(job_name)
    if not isinstance(job, dict):
        raise JobError(f"沒有 job `{job_name}`")

    unknown = set(job) - JOB_KEYS
    if unknown:
        raise JobError(f"job 有不認得的鍵：{', '.join(sorted(unknown))}")
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
            action = str(step["uses"]).split("@", 1)[0]
            options = step.get("with") or {}
            if action == "actions/checkout":
                # The workflow has already checked out this commit; options
                # would mean a different checkout, which is not replicated.
                if options:
                    raise JobError(f"{where}：actions/checkout 帶了選項，這裡不重現")
                continue
            if action == "actions/setup-go":
                if go_version_file is not None:
                    raise JobError(f"{where}：第二個 actions/setup-go")
                if steps:
                    raise JobError(f"{where}：actions/setup-go 在 run 步驟之後")
                if set(options) - {"go-version-file", "cache"}:
                    raise JobError(f"{where}：actions/setup-go 只認 go-version-file")
                go_version_file = relative(f"{where} go-version-file", options.get("go-version-file"))
                continue
            raise JobError(f"{where} 用了 {action}；這裡只認 actions/checkout 與 actions/setup-go")

        run = step.get("run")
        if not isinstance(run, str) or not run.strip():
            raise JobError(f"{where} 沒有 run")
        step_env = step.get("env") or {}
        if not isinstance(step_env, dict):
            raise JobError(f"{where} 的 env 不是 mapping")

        if any(isinstance(v, str) and CREDENTIAL.search(v) for v in step_env.values()):
            notes.append(f"{label_of(step, run)}：由 hoshi-ci 的 module 下載步驟代替")
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
            label=label_of(step, run),
            shell=shell,
            script=run,
            env={str(k): env_value(v) for k, v in step_env.items()},
            cwd=cwd,
        ))

    if go_version_file is None:
        raise JobError("沒有 actions/setup-go 的 go-version-file")
    if not steps:
        raise JobError("沒有任何 run 步驟")
    return Plan(go_version_file, timeout, env, steps, notes)


def load_plan(src: Path, workflow: str, job: str) -> Plan:
    import yaml  # installed, pinned, by the workflow step before this one

    path = src / workflow
    if not path.is_file():
        raise JobError(f"{workflow} 不存在")
    return build_plan(yaml.safe_load(path.read_text(encoding="utf-8")), job)


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
    env["GITHUB_WORKSPACE"] = str(src.resolve())
    for name in FILE_COMMANDS:
        env[name] = str(scratch / f"discard-{name.lower()}")
    env.update(plan.env)
    env.update(step.env)
    return env


def shown_lines(text: str) -> tuple[list[str], int]:
    lines = text.splitlines()
    shown = [line for line in lines if SHOWN.match(line)]
    return shown[:SHOWN_LIMIT], len(lines) - min(len(shown), SHOWN_LIMIT)


def kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        proc.kill()
    proc.wait()


def execute(plan: Plan, src: Path) -> int:
    scratch = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "hoshi-ci-job"
    scratch.mkdir(parents=True, exist_ok=True)

    for note in plan.notes:
        print(f"· {note}")

    deadline = time.monotonic() + plan.timeout_minutes * 60
    base = dict(os.environ)
    rows: list[tuple[str, str, str]] = []
    failed = False

    for index, step in enumerate(plan.steps, 1):
        if failed:
            rows.append((step.label, "未執行", ""))
            print(f"- {step.label}  未執行")
            continue

        cwd = src / step.cwd
        log = scratch / f"step{index}.log"
        start = time.monotonic()
        code: int | None = None
        reason = ""
        if not cwd.is_dir():
            reason = f"working-directory {step.cwd} 不存在"
        elif deadline - start <= 0:
            reason = f"超過 timeout-minutes（{plan.timeout_minutes}）"
        else:
            with log.open("wb") as out:
                proc = subprocess.Popen(
                    command(step, scratch, index),
                    cwd=cwd,
                    env=child_env(base, plan, step, src, scratch),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                )
                try:
                    code = proc.wait(timeout=deadline - start)
                except subprocess.TimeoutExpired:
                    kill_tree(proc)
                    reason = f"超過 timeout-minutes（{plan.timeout_minutes}）"
        elapsed = f"{time.monotonic() - start:.0f}s"

        if code == 0:
            rows.append((step.label, "✓", elapsed))
            print(f"✓ {step.label}  ({elapsed})")
            continue

        failed = True
        reason = reason or f"結束碼 {code}"
        rows.append((step.label, f"✗ {reason}", elapsed))
        print(f"✗ {step.label}  ({elapsed}，{reason})")
        print(f"::error::{step.label}：{reason}")
        if log.is_file():
            shown, hidden = shown_lines(log.read_bytes().decode("utf-8", errors="replace"))
            for line in shown:
                print(f"    {line}")
            if hidden:
                print(f"    （另有 {hidden} 行不公開）")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as out:
            out.write(f"### {JOB}\n\n| 步驟 | 結果 | 時間 |\n|---|---|---|\n")
            for label, result, elapsed in rows:
                out.write(f"| `{label}` | {result} | {elapsed} |\n")
            out.write("\n完整輸出不公開。要看失敗的細節，在 Windows 開發機 checkout 同一個 commit，"
                      "重跑失敗的那一步。\n")
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
    assert [s.label for s in plan.steps] == ["go build ./...", "cross"], plan.steps
    assert [s.shell for s in plan.steps] == ["pwsh", "bash"]
    assert [s.cwd for s in plan.steps] == ["backend", "backend"]
    assert plan.steps[1].env == {"GOOS": "linux", "CGO_ENABLED": "0"}
    assert len(plan.notes) == 1 and "私有 module 憑證" in plan.notes[0]

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
    }
    for what, mutate in rejected.items():
        try:
            build_plan(variant(mutate))
        except JobError:
            continue
        raise AssertionError(f"應該拒絕：{what}")
    try:
        build_plan({"jobs": {}})
    except JobError:
        pass
    else:
        raise AssertionError("應該拒絕：沒有那個 job")

    wrapped = pwsh_script("go build ./...\ngo vet ./...\n")
    assert wrapped.startswith("$ErrorActionPreference = 'stop'\n")
    assert wrapped.endswith("{ exit $LASTEXITCODE }\n")

    output = "\n".join([
        "=== RUN   TestA",
        "    a_test.go:12: expected secret-looking value, got other",
        "--- FAIL: TestA (0.00s)",
        "    --- FAIL: TestA/sub (0.00s)",
        "FAIL",
        "FAIL\tgithub.com/hoshivel/x/internal/a\t0.123s",
        "ok  \tgithub.com/hoshivel/x/internal/b\t0.1s",
        "# github.com/hoshivel/x/internal/c",
        "internal/c/c.go:3:2: undefined: token",
        "go: downloading example.com/m v1.0.0",
        "::error::a.ps1 語法錯誤",
        "panic: runtime error",
    ])
    shown, hidden = shown_lines(output)
    assert shown == [
        "--- FAIL: TestA (0.00s)",
        "    --- FAIL: TestA/sub (0.00s)",
        "FAIL",
        "FAIL\tgithub.com/hoshivel/x/internal/a\t0.123s",
        "# github.com/hoshivel/x/internal/c",
        "go: downloading example.com/m v1.0.0",
        "::error::a.ps1 語法錯誤",
    ], shown
    assert hidden == 5, hidden

    scratch = Path(tempfile.gettempdir())
    env = child_env({"GITHUB_STEP_SUMMARY": "real", "PATH": "p"}, plan, plan.steps[1], Path("."), scratch)
    assert env["GITHUB_STEP_SUMMARY"] != "real"
    assert env["GOOS"] == "linux" and env["GOPRIVATE"] == "github.com/hoshivel/*" and env["PATH"] == "p"

    print("run-job.py selftest：全部通過")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("plan", "run", "selftest"))
    parser.add_argument("--src", type=Path, help="被驗證倉庫 checkout 的位置")
    parser.add_argument("--workflow", default=WORKFLOW)
    parser.add_argument("--job", default=JOB)
    parser.add_argument("--output", help="plan 的 key=value 附加到這個檔（$GITHUB_OUTPUT）")
    args = parser.parse_args(argv)

    if args.mode == "selftest":
        return selftest()
    if args.src is None:
        parser.error("plan 與 run 需要 --src")

    try:
        plan = load_plan(args.src, args.workflow, args.job)
    except JobError as err:
        print(f"::error::{args.workflow} 的 {args.job}：{err}")
        return 1

    if args.mode == "plan":
        lines = f"go-version-file={plan.go_version_file}\nmodule-dir={plan.module_dir}\n"
        if args.output:
            with open(args.output, "a", encoding="utf-8") as out:
                out.write(lines)
        print(lines, end="")
        return 0
    try:
        return execute(plan, args.src)
    except JobError as err:
        print(f"::error::{err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
