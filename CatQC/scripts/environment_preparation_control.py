from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import atomic_write_json, load_json, sha256_file


SCHEMA_VERSION = 4
SECRET_IN_URL = re.compile(r"password|passwd|token|secret|api[_-]?key|access[_-]?key", re.I)
GUIDE_KINDS = {"repository_readme", "environment_file", "install_doc", "model_card", "package_doc", "release_note"}
AUTHORITIES = {"official_repository", "official_model_card", "official_package_docs", "official_release"}


class PreparationError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def model_context(model_root: Path) -> tuple[Path, str]:
    model_root = model_root.resolve()
    if model_root.name in {"", "."} or model_root.parent.name != "models":
        raise PreparationError("model_root must be benchmark_root/models/<model_key>")
    return model_root.parent.parent, model_root.name


def record_path(model_root: Path) -> Path:
    return model_root / "environment_preparation.json"


def relative_to_run(path: Path, run_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(run_root.resolve())).replace("\\", "/")
    except ValueError as exc:
        raise PreparationError(f"artifact must be inside benchmark_root: {path}") from exc


def load_record(model_root: Path) -> dict[str, Any]:
    path = record_path(model_root)
    if path.is_file():
        record = load_json(path)
        if record.get("schema_version") != SCHEMA_VERSION:
            raise PreparationError("environment_preparation.json must use schema_version 4")
        return record
    _, model_key = model_context(model_root)
    return {
        "schema_version": SCHEMA_VERSION, "model_key": model_key, "official_guides": [],
        "host_snapshot": None, "toolchain_decision": None,
        "mutation_authorization": {"environment_creation_allowed": False, "package_install_allowed": False, "uv_install_allowed": False},
        "status": "blocked",
    }


def save_record(model_root: Path, record: dict[str, Any]) -> None:
    atomic_write_json(record_path(model_root), record)


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent)
    os.close(fd)
    temporary_path = Path(temporary)
    try:
        shutil.copyfile(source, temporary_path)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def validate_source_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https", "file"} or not parsed.netloc and parsed.scheme != "file":
        raise PreparationError("source_url must be an http(s) or file URL")
    if SECRET_IN_URL.search(url):
        raise PreparationError("source_url appears to contain a credential-like query or field")


def host_matches(host: str, allowed_hosts: list[str]) -> bool:
    host = host.casefold().rstrip(".")
    return any(host == allowed.casefold().lstrip(".").rstrip(".") or host.endswith("." + allowed.casefold().lstrip(".")) for allowed in allowed_hosts)


def capture_guide(args: argparse.Namespace) -> dict[str, Any]:
    model_root = args.model_root.resolve()
    run_root, model_key = model_context(model_root)
    if args.kind not in GUIDE_KINDS or args.authority not in AUTHORITIES:
        raise PreparationError("unsupported guide kind or authority")
    validate_source_url(args.source_url)
    if not args.official_host:
        raise PreparationError("--official-host is required to bind the guide to an approved source host")
    source_host = urllib.parse.urlparse(args.source_url).hostname or ""
    if source_host and not host_matches(source_host, args.official_host):
        raise PreparationError("source_url host is not in --official-host")
    if not args.used_for:
        raise PreparationError("--used-for is required at least once")
    model_root.mkdir(parents=True, exist_ok=True)
    guide_scope = getattr(args, "guide_scope", "environment")
    if guide_scope not in {"environment", "usage", "both"}:
        raise PreparationError("guide_scope must be environment, usage, or both")
    guide_root = model_root / "resources" / "official_guides" / ("usage" if guide_scope == "usage" else "")
    guide_root.mkdir(parents=True, exist_ok=True)
    parsed = urllib.parse.urlparse(args.source_url)
    name = Path(parsed.path).name or "official-guide"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:100]
    if not Path(name).suffix:
        name += ".txt"
    stem = Path(name).stem
    suffix = Path(name).suffix
    name = f"{stem}-{hashlib.sha256(args.source_url.encode('utf-8')).hexdigest()[:10]}{suffix}"
    destination = guide_root / name
    capture_method = "http_fetched"
    final_url = args.source_url
    http_status: int | None = None
    content_type: str | None = None
    etag: str | None = None
    if args.source_path:
        if args.attested_by != "user":
            raise PreparationError("--source-path requires --attested-by user")
        source = args.source_path.resolve()
        if not source.is_file():
            raise PreparationError(f"guide source does not exist: {source}")
        atomic_copy(source, destination)
        capture_method = "local_user_attested"
    else:
        request = urllib.request.Request(args.source_url, headers={"User-Agent": "catqc/4"})
        with urllib.request.urlopen(request, timeout=30) as response:
            final_url = response.geturl()
            http_status = getattr(response, "status", None)
            content_type = response.headers.get("Content-Type")
            etag = response.headers.get("ETag")
            if urllib.parse.urlparse(final_url).scheme != "https":
                raise PreparationError("official guide capture requires an HTTPS final URL")
            final_host = urllib.parse.urlparse(final_url).hostname or ""
            if not host_matches(final_host, args.official_host):
                raise PreparationError("redirected final URL host is not in --official-host")
            fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent)
            os.close(fd)
            temporary_path = Path(temporary)
            try:
                handle = temporary_path.open("wb")
                total = 0
                with handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > 25 * 1024 * 1024:
                            raise PreparationError("official guide exceeds the 25 MiB capture limit")
                        handle.write(chunk)
                os.replace(temporary_path, destination)
            finally:
                temporary_path.unlink(missing_ok=True)
    record = load_record(model_root)
    artifact = {
        "kind": args.kind, "source_url": args.source_url, "revision": args.revision,
        "retrieved_at": now(), "artifact_path": relative_to_run(destination, run_root),
        "content_sha256": sha256_file(destination), "authority": args.authority,
        "used_for": args.used_for, "commands_preserved_verbatim": True,
        "capture_method": capture_method,
        "verification_status": "http_response_recorded" if capture_method == "http_fetched" else "user_attested_local_source",
        "final_url": final_url, "http_status": http_status,
        "content_type": content_type or mimetypes.guess_type(str(destination))[0], "etag": etag,
        "official_hosts": args.official_host,
        "scope": guide_scope,
    }
    record["official_guides"] = [item for item in record.get("official_guides", []) if item.get("artifact_path") != artifact["artifact_path"]]
    record["official_guides"].append(artifact)
    record["status"] = "guides_collected"
    save_record(model_root, record)
    return {"model_key": model_key, "artifact": artifact, "record_path": relative_to_run(record_path(model_root), run_root)}


def select_toolchain(args: argparse.Namespace) -> dict[str, Any]:
    model_root = args.model_root.resolve()
    run_root, model_key = model_context(model_root)
    snapshot_source = args.host_snapshot.resolve()
    if not snapshot_source.is_file():
        raise PreparationError(f"host snapshot does not exist: {snapshot_source}")
    snapshot_destination = model_root / "environment_preparation.host_snapshot.json"
    snapshot_destination.parent.mkdir(parents=True, exist_ok=True)
    if snapshot_source != snapshot_destination:
        atomic_copy(snapshot_source, snapshot_destination)
    snapshot = load_json(snapshot_destination)
    snapshot["snapshot_path"] = relative_to_run(snapshot_destination, run_root)
    snapshot["snapshot_sha256"] = sha256_file(snapshot_destination)
    record = load_record(model_root)
    record["host_snapshot"] = snapshot
    managers = snapshot.get("package_managers") or {}
    agent_candidates = snapshot.get("agent_runtime_candidates") or []
    alternatives: list[dict[str, Any]] = []
    selected = "user_decision_pending"
    owner = "none"
    executable = None
    version = None
    reason = "no user/agent uv or conda/mamba was detected"
    uv = managers.get("uv") or {}
    if uv.get("available") and uv.get("path"):
        if not Path(str(uv["path"])).is_file():
            raise PreparationError("probe reported uv but its executable path no longer exists")
        selected, owner, executable = "uv", "user", uv.get("path")
        reason = "user-owned uv is available in the probed execution context"
        alternatives.append({"tool": "uv", "owner": "user", "available": True, "path": executable})
    else:
        alternatives.append({"tool": "uv", "owner": "user", "available": False, "path": uv.get("path")})
        agent_uv = next((item for item in agent_candidates if item.get("path")), None)
        if agent_uv:
            if not Path(str(agent_uv.get("path"))).is_file():
                raise PreparationError("agent uv candidate path does not exist")
            selected, owner, executable = "uv", "agent", agent_uv.get("path")
            reason = "agent-owned uv is available and user-owned uv was not detected"
            alternatives.append({"tool": "uv", "owner": "agent", "available": True, "path": executable})
        else:
            alternatives.append({"tool": "uv", "owner": "agent", "available": False})
            for tool in ("conda", "mamba"):
                candidate = managers.get(tool) or {}
                alternatives.append({"tool": tool, "owner": "user", "available": bool(candidate.get("available")), "path": candidate.get("path")})
                if selected == "user_decision_pending" and candidate.get("available") and candidate.get("path"):
                    selected, owner, executable = tool, "user", candidate.get("path")
                    reason = f"{tool} is available after user/agent uv checks"
    for item in alternatives:
        if item.get("tool") == selected and item.get("path"):
            probe = managers.get(selected) or {}
            version_probe = probe.get("version_probe") or {}
            version = str(version_probe.get("stdout") or "").strip() or None
            break
    rank = {"uv": 1 if owner == "user" else 2, "conda": 3, "mamba": 3, "user_decision_pending": 4}[selected]
    record["toolchain_decision"] = {"selected": selected, "priority_rank": rank, "owner": owner,
                                     "executable": executable, "version": version, "reason": reason,
                                     "alternatives_checked": alternatives}
    record["status"] = "toolchain_selected" if selected != "user_decision_pending" else "blocked"
    save_record(model_root, record)
    return {"model_key": model_key, "toolchain_decision": record["toolchain_decision"], "status": record["status"]}


def freeze_plan(args: argparse.Namespace) -> dict[str, Any]:
    model_root = args.model_root.resolve()
    run_root, model_key = model_context(model_root)
    plan_source = args.plan_source.resolve()
    if not plan_source.is_file():
        raise PreparationError(f"installation plan does not exist: {plan_source}")
    plan = load_json(plan_source)
    required = {"source_guide_paths", "python", "commands", "packages", "environment_variables", "smoke_tests"}
    missing = sorted(required - set(plan))
    if missing:
        raise PreparationError(f"installation plan is missing fields: {missing}")
    record = load_record(model_root)
    guide_paths = {item.get("artifact_path") for item in record.get("official_guides", [])}
    if not set(plan["source_guide_paths"]).issubset(guide_paths):
        raise PreparationError("installation plan references an unrecorded official guide")
    destination = model_root / "resources" / "installation_plans" / "installation_plan.json"
    atomic_copy(plan_source, destination)
    record["installation_plan"] = {
        **plan, "artifact_path": relative_to_run(destination, run_root), "sha256": sha256_file(destination),
    }
    record["status"] = "toolchain_selected" if record.get("toolchain_decision") else "guides_collected"
    save_record(model_root, record)
    return {"model_key": model_key, "artifact_path": record["installation_plan"]["artifact_path"], "sha256": record["installation_plan"]["sha256"]}


def audit_record(model_root: Path, *, require_plan: bool = True) -> list[str]:
    run_root, model_key = model_context(model_root)
    record = load_record(model_root)
    errors: list[str] = []
    if record.get("model_key") != model_key:
        errors.append("model_key mismatch")
    guides = record.get("official_guides") or []
    if not guides:
        errors.append("no official environment guide is recorded")
    for guide in guides:
        path = run_root / str(guide.get("artifact_path") or "")
        if not path.is_file():
            errors.append(f"missing official guide artifact: {guide.get('artifact_path')}")
        elif sha256_file(path) != guide.get("content_sha256"):
            errors.append(f"official guide hash mismatch: {guide.get('artifact_path')}")
        if guide.get("capture_method") == "http_fetched" and guide.get("verification_status") != "http_response_recorded":
            errors.append(f"HTTP guide lacks response verification: {guide.get('artifact_path')}")
        allowed_hosts = [str(host) for host in guide.get("official_hosts") or []]
        for field in ("source_url", "final_url"):
            host = urllib.parse.urlparse(str(guide.get(field) or "")).hostname or ""
            if not allowed_hosts or not host_matches(host, allowed_hosts):
                errors.append(f"{field} host is outside official_hosts: {guide.get('artifact_path')}")
    snapshot = record.get("host_snapshot") or {}
    snapshot_path = run_root / str(snapshot.get("snapshot_path") or "")
    if not snapshot_path.is_file() or sha256_file(snapshot_path) != snapshot.get("snapshot_sha256"):
        errors.append("host snapshot is missing or hash-mismatched")
    decision = record.get("toolchain_decision") or {}
    if decision.get("selected") == "user_decision_pending":
        if record.get("status") != "blocked":
            errors.append("pending toolchain must block environment creation")
    elif not decision.get("executable") or decision.get("owner") not in {"user", "agent"}:
        errors.append("selected toolchain lacks an executable and owner")
    elif not Path(str(decision["executable"])).is_file():
        errors.append("selected toolchain executable no longer exists")
    if require_plan:
        plan = record.get("installation_plan") or {}
        plan_path = run_root / str(plan.get("artifact_path") or "")
        if not plan_path.is_file() or sha256_file(plan_path) != plan.get("sha256"):
            errors.append("installation plan is missing or hash-mismatched")
        guide_paths = {item.get("artifact_path") for item in guides}
        if not set(plan.get("source_guide_paths") or []).issubset(guide_paths):
            errors.append("installation plan references an unrecorded official guide")
        for field in ("commands", "smoke_tests"):
            if not isinstance(plan.get(field), list) or not plan.get(field):
                errors.append(f"installation plan {field} must be a nonempty list")
    auth = record.get("mutation_authorization") or {}
    if auth.get("uv_install_allowed") and not auth.get("approved_by"):
        errors.append("uv_install_allowed requires approved_by")
    return errors


def validate_record(args: argparse.Namespace) -> dict[str, Any]:
    model_root = args.model_root.resolve()
    run_root, model_key = model_context(model_root)
    record = load_record(model_root)
    errors = audit_record(model_root)
    result = {"valid": not errors, "model_key": model_key, "errors": errors, "status": record.get("status")}
    if not errors and record.get("status") == "toolchain_selected":
        record["status"] = "ready_for_creation"
        save_record(model_root, record)
        result["status"] = record["status"]
    if errors:
        raise PreparationError("; ".join(errors))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Control auditable model environment preparation")
    sub = parser.add_subparsers(dest="command", required=True)
    guide = sub.add_parser("capture-guide")
    guide.add_argument("model_root", type=Path)
    guide.add_argument("--source-url", required=True)
    guide.add_argument("--source-path", type=Path)
    guide.add_argument("--kind", required=True)
    guide.add_argument("--authority", required=True)
    guide.add_argument("--revision", default=None)
    guide.add_argument("--used-for", action="append", required=True)
    guide.add_argument("--attested-by", choices=["user"])
    guide.add_argument("--official-host", action="append", required=True)
    guide.add_argument("--guide-scope", choices=["environment", "usage", "both"], default="environment")
    select = sub.add_parser("select-toolchain")
    select.add_argument("model_root", type=Path)
    select.add_argument("--host-snapshot", type=Path, required=True)
    plan = sub.add_parser("freeze-plan")
    plan.add_argument("model_root", type=Path)
    plan.add_argument("--plan-source", type=Path, required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("model_root", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "capture-guide":
            result = capture_guide(args)
        elif args.command == "select-toolchain":
            result = select_toolchain(args)
        elif args.command == "freeze-plan":
            result = freeze_plan(args)
        else:
            result = validate_record(args)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
