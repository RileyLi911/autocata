from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import atomic_write_json, load_json
from environment_preparation_control import PreparationError, audit_record, load_record, model_context, save_record


SCHEMA_VERSION = 4


class InstallError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def execution_path(model_root: Path) -> Path:
    return model_root / "environment_execution.json"


def load_plan(model_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    record = load_record(model_root)
    errors = audit_record(model_root, require_plan=True)
    if errors:
        raise InstallError("environment preparation is not valid: " + "; ".join(errors))
    run_root, _ = model_context(model_root)
    plan_ref = record["installation_plan"]
    plan = load_json(run_root / plan_ref["artifact_path"])
    if plan.get("gpu_required") is not True or plan.get("framework") not in {"pytorch", "tensorflow", "jax"}:
        raise InstallError("installation plan must declare gpu_required=true and framework")
    return record, plan


def environment_python(environment_path: Path) -> Path:
    candidates = [environment_path / "Scripts" / "python.exe", environment_path / "bin" / "python", environment_path / "python.exe"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise InstallError(f"environment Python executable was not found under {environment_path}")


def exact_python_version(plan: dict[str, Any]) -> str:
    value = str((plan.get("python") or {}).get("version") or "").strip()
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", value):
        raise InstallError("installation plan python.version must be an exact version such as 3.11 or 3.11.9")
    return value


def base_create_command(record: dict[str, Any], plan: dict[str, Any], environment_path: Path) -> list[str]:
    decision = record["toolchain_decision"]
    tool = decision["selected"]
    executable = str(decision["executable"])
    python_version = exact_python_version(plan)
    if tool == "uv":
        return [executable, "venv", str(environment_path), "--python", python_version]
    return [executable, "create", "--prefix", str(environment_path), f"python={python_version}", "--yes"]


def command_record(argv: list[str], purpose: str, status: str = "planned", result: dict[str, Any] | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"argv": argv, "purpose": purpose, "status": status, "started_at": now()}
    if result:
        item["returncode"] = result.get("returncode")
        item["stdout"] = result.get("stdout", "")[-4000:]
        item["stderr"] = result.get("stderr", "")[-4000:]
        item["finished_at"] = now()
    return item


def run_argv(argv: list[str], cwd: Path | None = None, env: dict[str, str] | None = None, timeout: int = 3600) -> dict[str, Any]:
    try:
        completed = subprocess.run(argv, cwd=str(cwd) if cwd else None, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"returncode": -1, "stdout": "", "stderr": str(exc)}
    return {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}


def shell_command_argv(command: str) -> list[str]:
    if not command.strip():
        raise InstallError("installation plan contains an empty command")
    # Commands are displayed and executed only after explicit authorization.
    # PowerShell is used on Windows; POSIX shells are used on remote/Linux hosts.
    if os.name == "nt":
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    return ["/bin/sh", "-lc", command]


def write_execution(model_root: Path, record: dict[str, Any]) -> None:
    atomic_write_json(execution_path(model_root), record)


def plan_run(model_root: Path, environment_path: Path) -> dict[str, Any]:
    record, plan = load_plan(model_root)
    tool = record["toolchain_decision"]["selected"]
    if tool == "user_decision_pending":
        raise InstallError("no environment toolchain is available; user decision to install uv is required")
    commands = [command_record(base_create_command(record, plan, environment_path), "create_environment")]
    for command in plan.get("commands") or []:
        commands.append(command_record(shell_command_argv(str(command)), "official_install_command"))
    for test in plan.get("smoke_tests") or []:
        commands.append(command_record(shell_command_argv(str(test)), "official_smoke_test"))
    commands.append(command_record(["<environment-python>", str(Path(__file__).with_name("gpu_runtime_probe.py")),
                                    "--framework", str(plan["framework"]), "--output", "validation/gpu_runtime_probe.json"],
                                   "gpu_runtime_probe"))
    execution = {"schema_version": SCHEMA_VERSION, "model_key": record["model_key"], "toolchain": tool,
                 "toolchain_executable": str(record["toolchain_decision"]["executable"]),
                 "environment_path": str(environment_path), "commands": commands, "runtime": None,
                 "status": "planned", "last_error": None, "updated_at": now()}
    write_execution(model_root, execution)
    return execution


def authorize(model_root: Path, approved_by: str, allow_creation: bool, allow_install: bool, policy: str = "ask_once") -> dict[str, Any]:
    if approved_by != "user":
        raise InstallError("environment mutation authorization requires approved_by=user")
    if policy not in {"dry_run", "ask_once", "auto_after_confirmation"}:
        raise InstallError("unsupported environment mutation policy")
    record = load_record(model_root)
    auth = record.get("mutation_authorization") or {}
    auth.update({"policy": policy, "environment_creation_allowed": bool(allow_creation), "package_install_allowed": bool(allow_install),
                 "approved_by": approved_by, "approved_at": now()})
    record["mutation_authorization"] = auth
    save_record(model_root, record)
    return auth


def execute(model_root: Path, environment_path: Path, execute_install: bool) -> dict[str, Any]:
    record, plan = load_plan(model_root)
    auth = record.get("mutation_authorization") or {}
    if not auth.get("environment_creation_allowed") or not auth.get("package_install_allowed"):
        raise InstallError("explicit user authorization for environment creation and package installation is required")
    execution = plan_run(model_root, environment_path)
    commands = execution["commands"]
    if not execute_install:
        return execution
    env = os.environ.copy()
    for key, value in (plan.get("environment_variables") or {}).items():
        env[str(key)] = str(value)
    for index, item in enumerate(commands):
        argv = list(item["argv"])
        if item["purpose"] == "gpu_runtime_probe":
            python = environment_python(environment_path)
            argv[0] = str(python)
            argv[-1] = str(model_root / "validation" / "gpu_runtime_probe.json")
        result = run_argv(argv, cwd=model_root.parent.parent, env=env)
        item.update(command_record(argv, item["purpose"], "completed" if result["returncode"] == 0 else "failed", result))
        write_execution(model_root, execution)
        if result["returncode"] != 0:
            execution["status"] = "failed"
            execution["last_error"] = f"command {index} failed with returncode {result['returncode']}"
            write_execution(model_root, execution)
            raise InstallError(execution["last_error"])
        if item["purpose"] == "create_environment":
            execution["status"] = "created"
            write_execution(model_root, execution)
    python = environment_python(environment_path)
    runtime_result = run_argv([str(python), "-c", "import json,sys; from importlib.metadata import packages_distributions; print(json.dumps({'python':sys.executable,'version':sys.version,'distributions':sorted(packages_distributions())}))"], env=env)
    if runtime_result["returncode"] != 0:
        raise InstallError("created environment Python probe failed: " + runtime_result["stderr"][-1000:])
    gpu_probe_path = model_root / "validation" / "gpu_runtime_probe.json"
    if not gpu_probe_path.is_file():
        raise InstallError("GPU runtime probe did not produce its evidence file")
    gpu_probe = load_json(gpu_probe_path)
    if gpu_probe.get("passed") is not True:
        raise InstallError("GPU runtime integrity probe failed")
    execution["runtime"] = {"python_probe": runtime_result["stdout"][-4000:], "python_executable": str(python),
                             "gpu_probe": gpu_probe}
    execution["status"] = "validated"
    execution["updated_at"] = now()
    write_execution(model_root, execution)
    return execution


def auto_create(model_root: Path, environment_path: Path, approved_by: str) -> dict[str, Any]:
    """One explicit user confirmation, followed by the full frozen-plan execution."""
    authorize(model_root, approved_by, allow_creation=True, allow_install=True, policy="auto_after_confirmation")
    return execute(model_root, environment_path, execute_install=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and validate a model environment from a frozen plan")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "create"):
        command = sub.add_parser(name)
        command.add_argument("model_root", type=Path)
        command.add_argument("--environment-path", type=Path, required=True)
        command.add_argument("--execute", action="store_true")
    auth = sub.add_parser("authorize")
    auth.add_argument("model_root", type=Path)
    auth.add_argument("--approved-by", required=True)
    auth.add_argument("--allow-creation", action="store_true")
    auth.add_argument("--allow-install", action="store_true")
    auth.add_argument("--policy", choices=["dry_run", "ask_once", "auto_after_confirmation"], default="ask_once")
    automatic = sub.add_parser("auto-create")
    automatic.add_argument("model_root", type=Path)
    automatic.add_argument("--environment-path", type=Path, required=True)
    automatic.add_argument("--approved-by", required=True)
    args = parser.parse_args()
    try:
        model_root = args.model_root.resolve()
        if args.command == "authorize":
            result = authorize(model_root, args.approved_by, args.allow_creation, args.allow_install, args.policy)
        elif args.command == "auto-create":
            result = auto_create(model_root, args.environment_path.resolve(), args.approved_by)
        elif args.command == "plan":
            result = plan_run(model_root, args.environment_path.resolve())
        else:
            result = execute(model_root, args.environment_path.resolve(), args.execute)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
