from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROBE_DEADLINE_SECONDS = 60
_deadline = 0.0


def redact(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if any(token in line.casefold() for token in ("password", "passwd", "token", "secret", "api_key", "private_key")):
            lines.append("[REDACTED SENSITIVE OUTPUT]")
        else:
            lines.append(line)
    return "\n".join(lines)


def command_output(command: list[str], timeout: int = 10) -> dict[str, object]:
    if _deadline and time.monotonic() >= _deadline:
        return {"available": None, "command": command, "error": "probe deadline exceeded"}
    executable = shutil.which(command[0])
    if not executable:
        return {"available": False, "command": command}
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception as exc:
        return {"available": True, "command": command, "error": str(exc)}
    return {"available": True, "executable": executable, "command": command,
            "returncode": result.returncode, "stdout": redact(result.stdout[-4000:]),
            "stderr": redact(result.stderr[-4000:])}


def executable_record(name: str, owner: str = "path") -> dict[str, object]:
    path = shutil.which(name)
    record: dict[str, object] = {"name": name, "owner": owner, "available": bool(path), "path": path}
    if path:
        record["version_probe"] = command_output([name, "--version"])
    return record


def probe() -> dict[str, object]:
    global _deadline
    _deadline = time.monotonic() + PROBE_DEADLINE_SECONDS
    package_versions: dict[str, str] = {}
    for package in ("torch", "tensorflow", "jax", "ase", "dpdispatcher"):
        try:
            package_versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(), "hostname": socket.gethostname(),
        "os": {"system": platform.system(), "release": platform.release(), "version": platform.version(),
               "machine": platform.machine(), "python": sys.version, "python_executable": sys.executable},
        "python_candidates": [{"path": sys.executable, "version": sys.version}],
        "cuda": {
            "nvidia_smi": command_output(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"]),
            "nvcc": command_output(["nvcc", "--version"]), "torch_version": package_versions.get("torch"),
            "tensorflow_version": package_versions.get("tensorflow"), "jax_version": package_versions.get("jax")},
        "package_managers": {"uv": executable_record("uv"), "conda": executable_record("conda"),
                              "mamba": executable_record("mamba"), "pip": executable_record("pip"),
                              "conda_env_list": command_output(["conda", "env", "list", "--json"])},
        "agent_runtime_candidates": [{"name": key, "path": os.environ.get(key), "owner": "agent"}
                                      for key in ("HERMES_UV", "OPENCLAW_UV", "AGENT_UV") if os.environ.get(key)],
        "filesystem": {"cwd": str(Path.cwd()), "cwd_writable": os.access(Path.cwd(), os.W_OK)},
        "network": {"http_proxy_configured": bool(os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY"))},
        "package_versions": package_versions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only host and environment probe")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(probe(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
