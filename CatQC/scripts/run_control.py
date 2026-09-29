from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import socket
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from common import (
    SUCCESS_STATUSES,
    assess_result_row,
    atomic_write_json,
    canonical_json_sha256,
    frame_paths,
    validate_frame_artifact,
    load_json,
    normalize_status,
    read_csv,
    sha256_file,
)


from catalysis_selection import FIELDS as CATALYSIS_FIELDS, validate_selection as validate_catalysis

SCHEMA_VERSION = 4
LEASE_SECONDS = 15 * 60
HEARTBEAT_SECONDS = 60
TASKS = ("single_point", "relaxation")


class ControlError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_time(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat()


def parse_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ControlError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def slugify(value: str, *, max_length: int = 32) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", value.strip()).strip("-").lower()
    text = re.sub(r"-+", "-", text)[:max_length].rstrip("-")
    if not text:
        raise ControlError(f"cannot create a filesystem slug from {value!r}")
    return text


def normalize_tasks(values: list[str]) -> list[str]:
    selected = set(values)
    if not selected or not selected.issubset(TASKS):
        raise ControlError("tasks must contain single_point, relaxation, or both")
    return [task for task in TASKS if task in selected]


def task_slug(tasks: list[str]) -> str:
    return "sp-relax" if tasks == list(TASKS) else ("sp" if tasks == ["single_point"] else "relax")


def task_label(task: str) -> str:
    return "SP" if task == "single_point" else "Relax"


def task_result_path(model_key: str, task: str) -> str:
    return f"models/{model_key}/results/adsorption_energy_{task_label(task)}.csv"


def task_error_path(model_key: str, task: str) -> str:
    return f"models/{model_key}/errors/{task}_errors.csv"


def task_job_path(model_key: str, task: str) -> str:
    return f"models/{model_key}/jobs/{task}_job_status.json"


def task_checkpoint_path(model_key: str, task: str) -> str:
    return f"models/{model_key}/state/{task}_checkpoint.json"


def require_v4(data: dict[str, Any], label: str) -> None:
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        if version in {2, 3}:
            raise ControlError(
                f"{label} uses unsupported schema_version {version}; v2/v3 runs cannot be resumed and must be restarted"
            )
        raise ControlError(f"{label} schema_version must be {SCHEMA_VERSION}, got {version!r}")


def validate_selection_payload(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    mode = payload.get("model_selection_mode")
    if mode not in {"matbench_top_n", "user_specified"}:
        raise ControlError("selection payload model_selection_mode is invalid")
    raw_models = payload.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise ControlError("selection payload must contain a nonempty models array")
    models: list[dict[str, Any]] = []
    keys: set[str] = set()
    for expected_index, raw in enumerate(raw_models, start=1):
        if not isinstance(raw, dict):
            raise ControlError(f"selection model {expected_index} must be an object")
        model_name = raw.get("model_name") or raw.get("confirmed_name")
        identity_url = raw.get("identity_url") or raw.get("confirmed_url")
        required = {"requested_name": raw.get("requested_name"), "model_name": model_name,
                    "identity_url": identity_url, "source_kind": raw.get("source_kind")}
        missing = [field for field, value in required.items() if value in {None, ""}]
        if missing:
            raise ControlError(f"selection model {expected_index} is missing {missing}")
        if raw.get("selection_index", expected_index) != expected_index:
            raise ControlError("selection_index values must be ordered and contiguous from 1 through N")
        model_key = slugify(str(raw.get("model_key") or model_name), max_length=64)
        if model_key in keys:
            raise ControlError(f"duplicate model_key: {model_key}")
        keys.add(model_key)
        rank = raw.get("snapshot_rank")
        if mode == "matbench_top_n" and rank != expected_index:
            raise ControlError("Matbench snapshot_rank values must be ordered and contiguous from 1 through N")
        if mode == "user_specified":
            if rank is not None:
                raise ControlError("user_specified models must have snapshot_rank=null")
            if raw.get("confirmed") is not True or raw.get("confirmed_by") != "user":
                raise ControlError(f"user_specified model {expected_index} lacks explicit user confirmation")
        selection_errors = validate_catalysis(raw, mode)
        if selection_errors:
            raise ControlError("; ".join(selection_errors))
        model = {
            **{field: raw[field] for field in CATALYSIS_FIELDS},
            "selection_index": expected_index,
            "snapshot_rank": rank,
            "requested_name": str(raw["requested_name"]),
            "model_key": model_key,
            "model_name": str(model_name),
            "version_or_variant": raw.get("version_or_variant"),
            "identity_url": str(identity_url),
            "source_kind": str(raw["source_kind"]),
            "license": raw.get("license"),
            "resources": raw.get("resources") or [],
            "confirmed": raw.get("confirmed") if mode == "user_specified" else None,
            "confirmed_by": raw.get("confirmed_by") if mode == "user_specified" else None,
            "confirmed_at": raw.get("confirmed_at") if mode == "user_specified" else None,
            "source_evidence": raw.get("source_evidence") or [],
        }
        model["identity_sha256"] = canonical_json_sha256(model)
        models.append(model)
    return mode, models


def build_models_manifest(payload: dict[str, Any], frozen_at: str) -> dict[str, Any]:
    mode, models = validate_selection_payload(payload)
    return {
        "schema_version": SCHEMA_VERSION,
        "model_selection_mode": mode,
        "frozen_at": frozen_at,
        "model_count": len(models),
        "selection_source": payload.get("selection_source") or mode,
        "models": models,
    }


def request_identity(
    dataset_identity: str,
    adsorbate: str,
    tasks: list[str],
    models_manifest: dict[str, Any],
    dft_reference_mode: str = "provided",
    *,
    include_dft_mode: bool = True,
) -> str:
    identity = {
        "dataset_identity": dataset_identity.strip(),
        "adsorbate": adsorbate.strip(),
        "requested_tasks": tasks,
        "model_selection_mode": models_manifest["model_selection_mode"],
        "models": [
            {
                "selection_index": model["selection_index"],
                "model_key": model["model_key"],
                "model_name": model["model_name"],
                "version_or_variant": model.get("version_or_variant"),
                "identity_url": model["identity_url"],
                "identity_sha256": model["identity_sha256"],
            }
            for model in models_manifest["models"]
        ],
    }
    if include_dft_mode:
        if dft_reference_mode not in {"provided", "generate_vasp"}:
            raise ControlError("dft_reference_mode must be provided or generate_vasp")
        identity["dft_reference_mode"] = dft_reference_mode
    return canonical_json_sha256(identity)


def append_event(root: Path, state: dict[str, Any], owner_id: str, event_type: str, **fields: Any) -> int:
    sequence = int(state.get("last_event_sequence") or 0) + 1
    record = {
        "schema_version": SCHEMA_VERSION,
        "sequence": sequence,
        "timestamp": iso_time(),
        "owner_id": owner_id,
        "event_type": event_type,
        **fields,
    }
    path = root / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    state["last_event_sequence"] = sequence
    return sequence


def synchronize_event_sequence(root: Path, state: dict[str, Any]) -> None:
    path = root / "events.jsonl"
    if not path.is_file():
        if int(state.get("last_event_sequence") or 0) != 0:
            raise ControlError("events.jsonl is missing but run_state records events")
        return
    expected = 1
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.endswith("\n"):
                raise ControlError(f"events.jsonl line {line_number} is truncated")
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ControlError(f"events.jsonl line {line_number} is invalid JSON") from exc
            if record.get("schema_version") != SCHEMA_VERSION or record.get("sequence") != expected:
                raise ControlError(f"events.jsonl sequence/schema error at line {line_number}")
            expected += 1
    tail = expected - 1
    recorded = int(state.get("last_event_sequence") or 0)
    if recorded > tail:
        raise ControlError("run_state event sequence is ahead of events.jsonl")
    # A valid appended event with a stale state is a recoverable atomicity window.
    state["last_event_sequence"] = tail


def copy_selection_evidence(stage: Path, payload: dict[str, Any]) -> list[dict[str, Any]]:
    evidence_dir = stage / "selection_evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(evidence_dir / "model_selection_input.json", payload)
    records = [{
        "path": "selection_evidence/model_selection_input.json",
        "size_bytes": (evidence_dir / "model_selection_input.json").stat().st_size,
        "sha256": sha256_file(evidence_dir / "model_selection_input.json"),
    }]
    for raw in payload["models"]:
        for source in raw["catalysis_assessment"]["sources"]:
            relative = f"selection_evidence/catalysis/{source['sha256']}.txt"
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source["content"].encode("utf-8"))
            if not any(item["path"] == relative for item in records):
                records.append({"path": relative, "size_bytes": destination.stat().st_size, "sha256": sha256_file(destination)})
    for index, item in enumerate(payload.get("evidence_files") or [], start=1):
        source = Path(str(item.get("path") if isinstance(item, dict) else item)).resolve()
        if not source.is_file():
            raise ControlError(f"selection evidence file does not exist: {source}")
        name = f"{index:03d}-{slugify(source.stem, max_length=48)}{source.suffix.lower()}"
        destination = evidence_dir / name
        shutil.copy2(source, destination)
        records.append({
            "path": f"selection_evidence/{name}",
            "size_bytes": destination.stat().st_size,
            "sha256": sha256_file(destination),
        })
    return records


def initial_checkpoint(run_id: str, model_key: str, task: str, requested: bool, now: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "model_key": model_key,
        "task": task,
        "frame_artifacts_required": True,
        "frame_directory_template": f"models/{model_key}/frames/{task}/<structure_index:08d>",
        "expected_frame_count": None,
        "status": "not_initialized" if requested else "not_requested",
        "generation": 0,
        "completed_indices": [],
        "failed_indices": [],
        "active_indices": [],
        "retry_counts": {},
        "next_index": None,
        "result_path": task_result_path(model_key, task),
        "error_path": task_error_path(model_key, task),
        "job_status_path": task_job_path(model_key, task),
        "last_result_sha256": None,
        "last_validated_at": None,
        "updated_at": now,
    }


def initial_model_state(model: dict[str, Any], tasks: list[str], now: str) -> dict[str, Any]:
    return {
        "selection_index": model["selection_index"],
        "model_name": model["model_name"],
        "environment_status": "pending",
        "checkpoint_status": "pending",
        "current_action": None,
        "last_error": None,
        "tasks": {
            task: {
                "status": "not_initialized" if task in tasks else "not_requested",
                "checkpoint_path": task_checkpoint_path(model["model_key"], task),
                "n_completed": 0,
                "n_failed": 0,
                "n_active": 0,
            }
            for task in TASKS
        },
        "last_updated": now,
    }


def create_run(
    workspace_base: Path,
    skill_root: Path,
    dataset_name: str,
    adsorbate: str,
    tasks: list[str],
    original_prompt: str,
    payload: dict[str, Any],
    owner_id: str,
    dataset_identity: str | None = None,
    dft_reference_mode: str = "provided",
) -> Path:
    now_dt = utc_now()
    now = iso_time(now_dt)
    models_manifest = build_models_manifest(payload, now)
    for model in models_manifest["models"]:
        scope = model["catalysis_assessment"]["scope"]
        if str(scope["adsorbate"]).casefold() != adsorbate.casefold() or not set(tasks).issubset(scope["requested_tasks"]):
            raise ControlError("catalysis assessment scope does not cover the requested adsorbate/tasks")
    model_count = models_manifest["model_count"]
    dataset_identity = (dataset_identity or dataset_name).strip()
    identity = request_identity(dataset_identity, adsorbate, tasks, models_manifest, dft_reference_mode)
    run_base = (workspace_base / "CatQC" / "benchmark_runs").resolve()
    run_base.mkdir(parents=True, exist_ok=True)
    base_name = f"{now_dt.astimezone().strftime('%Y%m%d-%H%M%S')}_{model_count}models_{slugify(adsorbate)}_{task_slug(tasks)}"
    final = run_base / base_name
    suffix = 2
    while final.exists():
        final = run_base / f"{base_name}-{suffix:02d}"
        suffix += 1
    stage = run_base / f".creating-{uuid.uuid4().hex}"
    stage.mkdir()
    try:
        for relative in ("control", "models", "logs", "summary", "selection_evidence", "dft"):
            (stage / relative).mkdir(exist_ok=True)
        evidence_records = copy_selection_evidence(stage, payload)
        models_manifest["selection_evidence"] = evidence_records
        models_hash = canonical_json_sha256(models_manifest)
        run_id = final.name
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "skill_name": "catqc",
            "created_at": now,
            "updated_at": now,
            "status": "initialized",
            "request": {
                "model_selection_mode": models_manifest["model_selection_mode"],
                "dataset_name": dataset_name,
                "dataset_identity": dataset_identity,
                "adsorbate": adsorbate,
                "model_count": model_count,
                "requested_tasks": tasks,
                "task_slug": task_slug(tasks),
                "dft_reference_mode": dft_reference_mode,
                "original_prompt": original_prompt,
            },
            "request_identity_sha256": identity,
            "models_manifest_sha256": models_hash,
            "skill_root": str(skill_root.resolve()),
            "workspace_base": str(workspace_base.resolve()),
            "run_base": str(run_base),
            "benchmark_root": str(final.resolve()),
            "models_manifest_path": "models_manifest.json",
            "run_state_path": "run_state.json",
            "events_path": "events.jsonl",
            "lease_path": "control/lease.json",
            "scientific_definition_hash": None,
        }
        state = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "request_identity_sha256": identity,
            "status": "initialized",
            "current_step": "step0",
            "completed_steps": [],
            "generation": 0,
            "frame_count": None,
            "adsorbate_validation": "pending",
            "submission_allowed": False,
            "reconciliation_required": False,
            "current_action": "initialize_run",
            "next_action": "inspect_inputs",
            "active_jobs": [],
            "progress": {
                "expected_frames": None,
                "completed_frames": 0,
                "failed_frames": 0,
                "active_frames": 0,
                "coverage_fraction": 0.0,
            },
            "dft_reference": {
                "mode": dft_reference_mode,
                "status": "provided_pending" if dft_reference_mode == "provided" else "not_prepared",
                "expected_calculations": None,
                "completed_calculations": 0,
                "failed_calculations": 0,
                "active_calculations": 0,
                "valid_frames": 0,
                "coverage_fraction": 0.0,
                "manifest_path": None,
                "profile_sha256": None,
                "partial_approval_path": None,
                "next_action": "validate_provided_dft" if dft_reference_mode == "provided" else "confirm_vasp_profile",
                "last_updated": now,
            },
            "pending_questions": [],
            "models": {
                model["model_key"]: initial_model_state(model, tasks, now)
                for model in models_manifest["models"]
            },
            "last_event_sequence": 0,
            "last_updated": now,
        }
        lease = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "owner_id": owner_id,
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "status": "active",
            "acquired_at": now,
            "heartbeat_at": now,
            "expires_at": iso_time(now_dt + timedelta(seconds=LEASE_SECONDS)),
            "heartbeat_interval_seconds": HEARTBEAT_SECONDS,
            "lease_timeout_seconds": LEASE_SECONDS,
        }
        atomic_write_json(stage / "models_manifest.json", models_manifest)
        atomic_write_json(stage / "run_manifest.json", manifest)
        for model in models_manifest["models"]:
            model_key = model["model_key"]
            for relative in ("state", "jobs", "results", "errors", "logs", "scripts", "checkpoints", "validation", "structures", "raw_outputs"):
                (stage / "models" / model_key / relative).mkdir(parents=True, exist_ok=True)
            for task in TASKS:
                checkpoint = initial_checkpoint(run_id, model_key, task, task in tasks, now)
                atomic_write_json(stage / task_checkpoint_path(model_key, task), checkpoint)
        atomic_write_json(stage / "control" / "lease.json", lease)
        append_event(stage, state, owner_id, "run_initialized", request_identity_sha256=identity)
        state["current_action"] = None
        state["completed_steps"] = ["step0"]
        state["last_updated"] = iso_time()
        atomic_write_json(stage / "run_state.json", state)
        os.replace(stage, final)
        return final
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def require_catqc_manifest(root: Path, manifest: dict[str, Any]) -> None:
    if manifest.get("skill_name") != "catqc":
        raise ControlError("This is not a CatQC run; resume historical runs with their original skill release")
    if not manifest.get("workspace_base"):
        raise ControlError("CatQC run is missing workspace_base")
    expected = (Path(manifest["workspace_base"]) / "CatQC" / "benchmark_runs").resolve()
    if (Path(str(manifest.get("run_base", ""))).resolve() != expected
            or root.resolve().parent != expected
            or Path(str(manifest.get("benchmark_root", ""))).resolve() != root.resolve()):
        raise ControlError("CatQC run must be the recorded direct child of <workspace_base>/CatQC/benchmark_runs")


def scan_matching_runs(run_base: Path, identity: str, legacy_provided_identity: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    matches: list[dict[str, Any]] = []
    incompatible: list[dict[str, Any]] = []
    if not run_base.is_dir():
        return matches, incompatible
    for child in sorted(run_base.iterdir()):
        if not child.is_dir() or child.name.startswith(".creating-"):
            continue
        manifest_path = child / "run_manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = load_json(manifest_path)
        except Exception:
            continue
        version = manifest.get("schema_version")
        if version != SCHEMA_VERSION:
            incompatible.append({"path": str(child), "schema_version": version})
            continue
        try:
            require_catqc_manifest(child, manifest)
        except ControlError as exc:
            incompatible.append({"path": str(child), "schema_version": version, "reason": str(exc)})
            continue
        recorded_identity = manifest.get("request_identity_sha256")
        legacy_match = (
            legacy_provided_identity is not None
            and "dft_reference_mode" not in (manifest.get("request") or {})
            and recorded_identity == legacy_provided_identity
        )
        if (recorded_identity != identity and not legacy_match) or manifest.get("status") == "complete":
            continue
        state_path = child / "run_state.json"
        state = load_json(state_path) if state_path.is_file() else {}
        if state.get("status") == "complete":
            continue
        task_summaries = [
            task
            for model_state in (state.get("models") or {}).values()
            for task in (model_state.get("tasks") or {}).values()
            if task.get("status") != "not_requested"
        ]
        completed_frames = sum(int(task.get("n_completed") or 0) for task in task_summaries)
        frame_count = state.get("frame_count")
        expected_frames = (int(frame_count) * len(task_summaries)) if frame_count else None
        matches.append({
            "path": str(child.resolve()),
            "status": state.get("status", manifest.get("status")),
            "current_step": state.get("current_step"),
            "last_updated": state.get("last_updated", manifest.get("updated_at")),
            "next_action": state.get("next_action"),
            "completed_frames": completed_frames,
            "expected_frames": expected_frames,
            "progress_fraction": (completed_frames / expected_frames) if expected_frames else 0.0,
        })
    matches.sort(key=lambda item: str(item.get("last_updated") or ""), reverse=True)
    return matches, incompatible


def load_core(root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest = load_json(root / "run_manifest.json")
    models = load_json(root / "models_manifest.json")
    state = load_json(root / "run_state.json")
    require_v4(manifest, "run_manifest.json")
    require_catqc_manifest(root, manifest)
    require_v4(models, "models_manifest.json")
    require_v4(state, "run_state.json")
    if manifest.get("models_manifest_sha256") != canonical_json_sha256(models):
        raise ControlError("models_manifest.json hash does not match run_manifest.json")
    if state.get("request_identity_sha256") != manifest.get("request_identity_sha256"):
        raise ControlError("run_state request identity does not match run_manifest")
    for model in models.get("models", []):
        selection_errors = validate_catalysis(model, models.get("model_selection_mode"))
        if selection_errors:
            raise ControlError("; ".join(selection_errors))
    for evidence in models.get("selection_evidence", []):
        path = root / evidence["path"]
        if not path.is_file() or sha256_file(path) != evidence.get("sha256"):
            raise ControlError("selection source evidence missing or changed: " + evidence["path"])
    synchronize_event_sequence(root, state)
    return manifest, models, state


def acquire_lease(root: Path, owner_id: str) -> tuple[dict[str, Any], bool]:
    manifest, _, state = load_core(root)
    path = root / "control" / "lease.json"
    now = utc_now()
    takeover = False
    if path.is_file():
        lease = load_json(path)
        require_v4(lease, "control/lease.json")
        active = lease.get("status") == "active" and parse_time(lease.get("expires_at")) > now
        if active and lease.get("owner_id") != owner_id:
            raise ControlError(
                f"run is controlled by active owner {lease.get('owner_id')}; submissions are forbidden until its lease expires or is released"
            )
        takeover = lease.get("owner_id") != owner_id and not active
        acquired_at = lease.get("acquired_at") if lease.get("owner_id") == owner_id else iso_time(now)
    else:
        acquired_at = iso_time(now)
    lease = {
        "schema_version": SCHEMA_VERSION,
        "run_id": manifest["run_id"],
        "owner_id": owner_id,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "status": "active",
        "acquired_at": acquired_at,
        "heartbeat_at": iso_time(now),
        "expires_at": iso_time(now + timedelta(seconds=LEASE_SECONDS)),
        "heartbeat_interval_seconds": HEARTBEAT_SECONDS,
        "lease_timeout_seconds": LEASE_SECONDS,
    }
    atomic_write_json(path, lease)
    if takeover:
        state["submission_allowed"] = False
        state["reconciliation_required"] = True
        state["next_action"] = "reconcile_external_jobs"
        append_event(root, state, owner_id, "lease_taken_over")
        state["last_updated"] = iso_time()
        atomic_write_json(root / "run_state.json", state)
    return lease, takeover


def require_owner(root: Path, owner_id: str) -> dict[str, Any]:
    require_catqc_manifest(root, load_json(root / "run_manifest.json"))
    path = root / "control" / "lease.json"
    if not path.is_file():
        raise ControlError("control/lease.json is missing")
    lease = load_json(path)
    require_v4(lease, "control/lease.json")
    if lease.get("status") != "active" or lease.get("owner_id") != owner_id:
        raise ControlError("the caller does not own the active run lease")
    if parse_time(lease.get("expires_at")) <= utc_now():
        raise ControlError("the caller lease has expired; reacquire and reconcile before writing state")
    return lease


def heartbeat(root: Path, owner_id: str) -> dict[str, Any]:
    lease = require_owner(root, owner_id)
    now = utc_now()
    lease["heartbeat_at"] = iso_time(now)
    lease["expires_at"] = iso_time(now + timedelta(seconds=LEASE_SECONDS))
    lease["pid"] = os.getpid()
    atomic_write_json(root / "control" / "lease.json", lease)
    return lease


def release_lease(root: Path, owner_id: str) -> dict[str, Any]:
    lease = require_owner(root, owner_id)
    _, _, state = load_core(root)
    lease["status"] = "released"
    lease["heartbeat_at"] = iso_time()
    lease["expires_at"] = lease["heartbeat_at"]
    atomic_write_json(root / "control" / "lease.json", lease)
    append_event(root, state, owner_id, "lease_released")
    state["last_updated"] = iso_time()
    atomic_write_json(root / "run_state.json", state)
    return lease


def initialize_frames(root: Path, owner_id: str, frame_count: int, observed_adsorbate: str) -> dict[str, Any]:
    require_owner(root, owner_id)
    manifest, models_manifest, state = load_core(root)
    expected_adsorbate = str(manifest["request"]["adsorbate"])
    if observed_adsorbate.strip().casefold() != expected_adsorbate.strip().casefold():
        state["status"] = "blocked"
        state["adsorbate_validation"] = "mismatch"
        state["submission_allowed"] = False
        state["pending_questions"] = [
            f"Requested adsorbate {expected_adsorbate!r} does not match observed {observed_adsorbate!r}"
        ]
        state["next_action"] = "resolve_adsorbate_mismatch"
        append_event(root, state, owner_id, "adsorbate_validation_failed", expected=expected_adsorbate, observed=observed_adsorbate)
        state["last_updated"] = iso_time()
        atomic_write_json(root / "run_state.json", state)
        raise ControlError(state["pending_questions"][0])
    if frame_count < 1:
        raise ControlError("frame_count must be positive")
    tasks = manifest["request"]["requested_tasks"]
    for model in models_manifest["models"]:
        for task in TASKS:
            path = root / task_checkpoint_path(model["model_key"], task)
            checkpoint = load_json(path)
            require_v4(checkpoint, str(path.relative_to(root)))
            checkpoint["expected_frame_count"] = frame_count
            checkpoint["status"] = "pending" if task in tasks else "not_requested"
            checkpoint["next_index"] = 0 if task in tasks else None
            checkpoint["generation"] = int(checkpoint.get("generation") or 0) + 1
            checkpoint["updated_at"] = iso_time()
            atomic_write_json(path, checkpoint)
            for index in range(frame_count):
                frame = frame_paths(root, model["model_key"], task, index)
                for directory_key in ("input", "work", "outputs", "errors"):
                    frame[directory_key].mkdir(parents=True, exist_ok=True)
            summary = state["models"][model["model_key"]]["tasks"][task]
            summary.update({
                "status": checkpoint["status"],
                "n_completed": 0,
                "n_failed": 0,
                "n_active": 0,
            })
    state["frame_count"] = frame_count
    expected_total = frame_count * len(models_manifest["models"]) * len(tasks)
    state["progress"] = {
        "expected_frames": expected_total,
        "completed_frames": 0,
        "failed_frames": 0,
        "active_frames": 0,
        "coverage_fraction": 0.0,
    }
    state["adsorbate_validation"] = "validated"
    state["status"] = "active"
    state["current_step"] = "step1"
    state["submission_allowed"] = False
    state["next_action"] = "prepare_model_resources"
    state["generation"] = int(state.get("generation") or 0) + 1
    append_event(root, state, owner_id, "frame_checkpoints_initialized", frame_count=frame_count)
    state["last_updated"] = iso_time()
    atomic_write_json(root / "run_state.json", state)
    config_path = root / "benchmark_config.json"
    if config_path.is_file():
        manifest["status"] = "active"
        manifest["scientific_definition_hash"] = canonical_json_sha256(load_json(config_path))
        manifest["updated_at"] = iso_time()
        atomic_write_json(root / "run_manifest.json", manifest)
    return state


def valid_result_indices(root: Path, checkpoint: dict[str, Any], config: dict[str, Any] | None) -> tuple[set[int], set[int], str | None]:
    path = root / checkpoint["result_path"]
    if not path.is_file():
        return set(), set(), None
    rows = read_csv(path)
    seen: set[int] = set()
    completed: set[int] = set()
    failed: set[int] = set()
    validation = (config or {}).get("result_validation") or {}
    relaxation = (config or {}).get("relaxation") or {}
    for row in rows:
        try:
            index = int(row.get("structure_index", ""))
        except ValueError as exc:
            raise ControlError(f"{checkpoint['result_path']} contains an invalid structure_index") from exc
        if index in seen:
            raise ControlError(f"{checkpoint['result_path']} contains duplicate structure_index {index}")
        seen.add(index)
        status = normalize_status(row.get("status"))
        if checkpoint.get("frame_artifacts_required", True):
            artifact = validate_frame_artifact(
                root, checkpoint["model_key"], checkpoint["task"], index,
                expected_run_id=checkpoint.get("run_id"),
                expected_frame_count=checkpoint.get("expected_frame_count"),
            )
            if not artifact["valid"]:
                raise ControlError(
                    f"frame artifact invalid for {checkpoint['model_key']}/{checkpoint['task']}/{index}: "
                    + "; ".join(artifact["errors"])
                )
            artifact_status = normalize_status((artifact.get("manifest") or {}).get("status"))
            if status == "success" and artifact_status != "success":
                raise ControlError(f"aggregate result status disagrees with frame manifest for frame {index}")
        if config and status == "success":
            assessment = assess_result_row(row, task_label(checkpoint["task"]), validation, relaxation)
            if assessment["valid"]:
                completed.add(index)
            else:
                failed.add(index)
        elif str(row.get("status") or "").strip().lower() in SUCCESS_STATUSES or status == "success":
            completed.add(index)
        elif status in {"failed", "unconverged"}:
            failed.add(index)
    return completed, failed, sha256_file(path)


def valid_error_indices(root: Path, checkpoint: dict[str, Any]) -> set[int]:
    path = root / checkpoint["error_path"]
    if not path.is_file():
        return set()
    indices: set[int] = set()
    for row in read_csv(path):
        try:
            index = int(row.get("structure_index", ""))
        except ValueError as exc:
            raise ControlError(f"{checkpoint['error_path']} contains an invalid structure_index") from exc
        if index in indices:
            raise ControlError(f"{checkpoint['error_path']} contains duplicate structure_index {index}")
        if not str(row.get("error_message") or row.get("reason") or "").strip():
            raise ControlError(f"{checkpoint['error_path']} frame {index} lacks an error reason")
        if checkpoint.get("frame_artifacts_required", True):
            artifact = validate_frame_artifact(
                root, checkpoint["model_key"], checkpoint["task"], index,
                expected_run_id=checkpoint.get("run_id"),
                expected_frame_count=checkpoint.get("expected_frame_count"),
            )
            if not artifact["valid"]:
                raise ControlError(
                    f"frame artifact invalid for error frame {index}: " + "; ".join(artifact["errors"])
                )
            if (artifact.get("manifest") or {}).get("status") not in {"failed", "unconverged"}:
                raise ControlError(f"error row status disagrees with frame manifest for frame {index}")
        indices.add(index)
    return indices


def load_job(root: Path, relative: str) -> dict[str, Any] | None:
    path = root / relative
    if not path.is_file():
        return None
    job = load_json(path)
    require_v4(job, relative)
    if job.get("state") in {"queued", "running"} and not (job.get("job_id") is not None or job.get("pid") is not None):
        raise ControlError(f"active job {relative} requires job_id or pid")
    return job


def reconcile_run(root: Path, owner_id: str, *, external_jobs_reconciled: bool = False) -> dict[str, Any]:
    require_owner(root, owner_id)
    manifest, models_manifest, state = load_core(root)
    config_path = root / "benchmark_config.json"
    config = load_json(config_path) if config_path.is_file() else None
    frame_count = state.get("frame_count")
    retry_limit = int(((config or {}).get("retry_policy") or {}).get("max_attempts") or 2)
    active_jobs: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    all_requested_complete = frame_count is not None
    for model in models_manifest["models"]:
        model_key = model["model_key"]
        model_state = state["models"][model_key]
        for task in TASKS:
            summary = model_state["tasks"][task]
            checkpoint_path = root / summary["checkpoint_path"]
            checkpoint = load_json(checkpoint_path)
            require_v4(checkpoint, summary["checkpoint_path"])
            if checkpoint["status"] == "not_requested":
                continue
            completed, result_failed, result_hash = valid_result_indices(root, checkpoint, config)
            expected = checkpoint.get("expected_frame_count")
            if expected is not None:
                outside = [index for index in completed | result_failed if index < 0 or index >= expected]
                if outside:
                    raise ControlError(f"{checkpoint['result_path']} has indices outside 0..{expected - 1}: {outside}")
            old_completed = set(checkpoint.get("completed_indices") or [])
            old_failed = set(checkpoint.get("failed_indices") or [])
            error_failed = valid_error_indices(root, checkpoint)
            failed = (result_failed | error_failed) - completed
            job = load_job(root, checkpoint["job_status_path"])
            active = set(checkpoint.get("active_indices") or [])
            if job and job.get("state") in {"queued", "running"}:
                active = set(job.get("active_indices") or [])
                active_jobs.append({
                    "model_key": model_key,
                    "task": task,
                    "job_id": job.get("job_id"),
                    "pid": job.get("pid"),
                    "state": job.get("state"),
                    "job_status_path": checkpoint["job_status_path"],
                })
            else:
                active = set()
            retry_counts = {str(key): int(value) for key, value in (checkpoint.get("retry_counts") or {}).items()}
            pending: list[int] = []
            if expected is not None:
                for index in range(expected):
                    if index in completed or index in active:
                        continue
                    if index in failed and retry_counts.get(str(index), 0) >= retry_limit:
                        continue
                    pending.append(index)
            if expected is None:
                status = "not_initialized"
                all_requested_complete = False
            elif len(completed) == expected:
                status = "complete"
            elif active:
                status = "running"
                all_requested_complete = False
            elif pending:
                status = "pending"
                all_requested_complete = False
            else:
                status = "failed"
                all_requested_complete = False
            checkpoint.update({
                "status": status,
                "completed_indices": sorted(completed),
                "failed_indices": sorted(failed),
                "active_indices": sorted(active),
                "next_index": pending[0] if pending else None,
                "last_result_sha256": result_hash,
                "last_validated_at": iso_time(),
                "generation": int(checkpoint.get("generation") or 0) + 1,
                "updated_at": iso_time(),
            })
            if completed != old_completed or failed != old_failed:
                changes.append({
                    "model_key": model_key,
                    "task": task,
                    "completed_added": sorted(completed - old_completed),
                    "completed_removed": sorted(old_completed - completed),
                    "failed_added": sorted(failed - old_failed),
                })
            atomic_write_json(checkpoint_path, checkpoint)
            summary.update({
                "status": status,
                "n_completed": len(completed),
                "n_failed": len(failed),
                "n_active": len(active),
            })
        model_state["last_updated"] = iso_time()
    reconciliation_was_required = bool(state.get("reconciliation_required"))
    task_summaries = [
        task_summary
        for model_state in state["models"].values()
        for task_summary in model_state["tasks"].values()
        if task_summary["status"] != "not_requested"
    ]
    expected_total = (int(frame_count) * len(task_summaries)) if frame_count is not None else None
    completed_total = sum(item["n_completed"] for item in task_summaries)
    failed_total = sum(item["n_failed"] for item in task_summaries)
    active_total = sum(item["n_active"] for item in task_summaries)
    if reconciliation_was_required and not external_jobs_reconciled:
        submission_allowed = False
        next_action = "reconcile_external_jobs"
    elif active_jobs:
        submission_allowed = False
        next_action = "monitor_active_jobs"
    elif state.get("adsorbate_validation") != "validated":
        submission_allowed = False
        next_action = "inspect_inputs"
    else:
        submission_allowed = True
        next_action = "continue_next_incomplete_frame"
    state.update({
        "active_jobs": active_jobs,
        "progress": {
            "expected_frames": expected_total,
            "completed_frames": completed_total,
            "failed_frames": failed_total,
            "active_frames": active_total,
            "coverage_fraction": (completed_total / expected_total) if expected_total else 0.0,
        },
        "submission_allowed": submission_allowed,
        "reconciliation_required": reconciliation_was_required and not external_jobs_reconciled,
        "next_action": "finalize_run" if all_requested_complete else next_action,
        "generation": int(state.get("generation") or 0) + 1,
    })
    report = {
        "schema_version": SCHEMA_VERSION,
        "run_id": manifest["run_id"],
        "reconciled_at": iso_time(),
        "owner_id": owner_id,
        "external_jobs_reconciled": external_jobs_reconciled,
        "submission_allowed": state["submission_allowed"],
        "active_jobs": active_jobs,
        "changes": changes,
        "next_action": state["next_action"],
    }
    timestamp = utc_now().strftime("%Y%m%d-%H%M%S-%f")
    report_path = root / "control" / f"reconciliation-{timestamp}.json"
    atomic_write_json(report_path, report)
    atomic_write_json(root / "control" / "latest_reconciliation.json", report)
    append_event(root, state, owner_id, "run_reconciled", report_path=str(report_path.relative_to(root)).replace("\\", "/"), changes=len(changes))
    state["last_updated"] = iso_time()
    atomic_write_json(root / "run_state.json", state)
    return report


def record_frame(root: Path, owner_id: str, model_key: str, task: str, index: int, outcome: str) -> dict[str, Any]:
    require_owner(root, owner_id)
    manifest, models_manifest, state = load_core(root)
    if model_key not in {model["model_key"] for model in models_manifest["models"]}:
        raise ControlError(f"unknown model_key: {model_key}")
    if task not in manifest["request"]["requested_tasks"]:
        raise ControlError(f"task {task} was not requested")
    path = root / task_checkpoint_path(model_key, task)
    checkpoint = load_json(path)
    expected = checkpoint.get("expected_frame_count")
    if expected is None or index < 0 or index >= expected:
        raise ControlError(f"structure_index {index} is outside the initialized frame range")
    config_path = root / "benchmark_config.json"
    config = load_json(config_path) if config_path.is_file() else None
    artifact = validate_frame_artifact(
        root, model_key, task, index,
        expected_run_id=checkpoint.get("run_id"),
        expected_frame_count=expected,
    )
    if not artifact["valid"]:
        raise ControlError("cannot record frame before its artifacts are valid: " + "; ".join(artifact["errors"]))
    manifest_status = normalize_status((artifact.get("manifest") or {}).get("status"))
    if outcome == "completed" and manifest_status != "success":
        raise ControlError("completed frame requires frame_manifest status=completed")
    if outcome == "failed" and manifest_status not in {"failed", "unconverged"}:
        raise ControlError("failed frame requires frame_manifest status=failed or unconverged")
    completed, failed_from_result, result_hash = valid_result_indices(root, checkpoint, config)
    failed_from_error = valid_error_indices(root, checkpoint)
    if outcome == "completed" and index not in completed:
        raise ControlError("cannot record completion before a valid result row is persisted")
    if outcome == "failed" and index not in failed_from_result and index not in failed_from_error:
        raise ControlError("cannot record failure before an error or failed result artifact is persisted")
    failed = set(checkpoint.get("failed_indices") or [])
    retries = {str(key): int(value) for key, value in (checkpoint.get("retry_counts") or {}).items()}
    if outcome == "completed":
        failed.discard(index)
    else:
        failed.add(index)
        retries[str(index)] = retries.get(str(index), 0) + 1
    checkpoint.update({
        "completed_indices": sorted(completed),
        "failed_indices": sorted(failed - completed),
        "active_indices": sorted(set(checkpoint.get("active_indices") or []) - {index}),
        "retry_counts": retries,
        "last_result_sha256": result_hash,
        "last_validated_at": iso_time(),
        "generation": int(checkpoint.get("generation") or 0) + 1,
        "updated_at": iso_time(),
    })
    atomic_write_json(path, checkpoint)
    append_event(root, state, owner_id, f"frame_{outcome}", model_key=model_key, task=task, structure_index=index)
    state["last_updated"] = iso_time()
    atomic_write_json(root / "run_state.json", state)
    return reconcile_run(root, owner_id)


def finalize_run(root: Path, owner_id: str) -> dict[str, Any]:
    require_owner(root, owner_id)
    manifest, _, state = load_core(root)
    # Recheck report freshness instead of trusting a cached completion audit.
    from model_selection_report import validate_report
    report_validation = validate_report(root, final=True)
    if not report_validation["passed"]:
        raise ControlError("model selection report is not ready: " + "; ".join(report_validation["errors"]))
    audit_path = root / "summary" / "completion_audit.json"
    if not audit_path.is_file() or load_json(audit_path).get("passed") is not True:
        raise ControlError("a passing summary/completion_audit.json is required before finalization")
    if state.get("active_jobs") or state.get("reconciliation_required"):
        raise ControlError("cannot finalize with active jobs or pending reconciliation")
    if "dft_reference_mode" in (manifest.get("request") or {}):
        dft_reference = state.get("dft_reference") or {}
        allowed_dft_statuses = {"provided_ready"} if dft_reference.get("mode", "provided") == "provided" else {"complete", "partial_approved"}
        if dft_reference.get("status") not in allowed_dft_statuses:
            raise ControlError("cannot finalize before the DFT reference branch is ready or explicitly approved")
    requested = [
        task
        for model_state in state["models"].values()
        for task in model_state["tasks"].values()
        if task["status"] != "not_requested"
    ]
    if not requested or any(task["status"] != "complete" for task in requested):
        raise ControlError("all requested task checkpoints must be complete before finalization")
    state.update({
        "status": "complete",
        "current_step": "complete",
        "completed_steps": ["step0", "step1", "step2", "step3", "step4", "step5"],
        "submission_allowed": False,
        "current_action": None,
        "next_action": None,
        "generation": int(state.get("generation") or 0) + 1,
    })
    append_event(root, state, owner_id, "run_completed")
    state["last_updated"] = iso_time()
    atomic_write_json(root / "run_state.json", state)
    manifest["status"] = "complete"
    manifest["updated_at"] = state["last_updated"]
    atomic_write_json(root / "run_manifest.json", manifest)
    return {"schema_version": SCHEMA_VERSION, "run_id": manifest["run_id"], "status": "complete"}


def init_or_resume(args: argparse.Namespace) -> dict[str, Any]:
    if args.new_run and args.select_run:
        raise ControlError("--new-run and --select-run are mutually exclusive")
    workspace_base = args.workspace_base.resolve()
    skill_root = args.skill_root.resolve()
    payload = load_json(args.selection_file.resolve())
    tasks = normalize_tasks(args.tasks)
    models_manifest = build_models_manifest(payload, "identity-only")
    dataset_identity = (getattr(args, "dataset_identity", None) or args.dataset_name).strip()
    dft_reference_mode = getattr(args, "dft_reference_mode", "provided")
    identity = request_identity(dataset_identity, args.adsorbate, tasks, models_manifest, dft_reference_mode)
    run_base = workspace_base / "CatQC" / "benchmark_runs"
    legacy_identity = request_identity(dataset_identity, args.adsorbate, tasks, models_manifest, include_dft_mode=False) if dft_reference_mode == "provided" else None
    matches, incompatible = scan_matching_runs(run_base, identity, legacy_identity)
    selected: Path | None = None
    if args.select_run:
        selected = args.select_run.resolve()
        if selected not in {Path(item["path"]).resolve() for item in matches}:
            raise ControlError("--select-run is not an incomplete v4 run matching this request")
    elif not args.new_run and len(matches) == 1:
        selected = Path(matches[0]["path"])
    elif not args.new_run and len(matches) > 1:
        return {"action": "selection_required", "request_identity_sha256": identity, "candidates": matches, "incompatible_runs": incompatible}
    if selected:
        _, takeover = acquire_lease(selected, args.owner_id)
        report = reconcile_run(selected, args.owner_id, external_jobs_reconciled=args.external_jobs_reconciled)
        return {"action": "resumed", "benchmark_root": str(selected), "lease_takeover": takeover, "reconciliation": report}
    created = create_run(
        workspace_base,
        skill_root,
        args.dataset_name,
        args.adsorbate,
        tasks,
        args.original_prompt,
        payload,
        args.owner_id,
        dataset_identity,
        dft_reference_mode,
    )
    return {"action": "created", "benchmark_root": str(created), "request_identity_sha256": identity, "incompatible_runs": incompatible}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Initialize and resume v4 adsorption benchmark runs")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-or-resume")
    init.add_argument("--workspace-base", type=Path, required=True)
    init.add_argument("--skill-root", type=Path, required=True)
    init.add_argument("--selection-file", type=Path, required=True)
    init.add_argument("--dataset-name", required=True)
    init.add_argument("--dataset-identity", help="Stable dataset ID/version/hash; defaults to --dataset-name")
    init.add_argument("--adsorbate", required=True)
    init.add_argument("--dft-reference-mode", choices=("provided", "generate_vasp"), default="provided")
    init.add_argument("--tasks", nargs="+", choices=TASKS, required=True)
    init.add_argument("--owner-id", required=True)
    init.add_argument("--original-prompt", default="")
    init.add_argument("--new-run", action="store_true")
    init.add_argument("--select-run", type=Path)
    init.add_argument("--external-jobs-reconciled", action="store_true")
    frames = sub.add_parser("initialize-frames")
    frames.add_argument("benchmark_root", type=Path)
    frames.add_argument("--owner-id", required=True)
    frames.add_argument("--frame-count", type=int, required=True)
    frames.add_argument("--observed-adsorbate", required=True)
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("benchmark_root", type=Path)
    reconcile.add_argument("--owner-id", required=True)
    reconcile.add_argument("--external-jobs-reconciled", action="store_true")
    frame = sub.add_parser("record-frame")
    frame.add_argument("benchmark_root", type=Path)
    frame.add_argument("--owner-id", required=True)
    frame.add_argument("--model-key", required=True)
    frame.add_argument("--task", choices=TASKS, required=True)
    frame.add_argument("--structure-index", type=int, required=True)
    frame.add_argument("--outcome", choices=["completed", "failed"], required=True)
    paths = sub.add_parser("frame-paths")
    paths.add_argument("benchmark_root", type=Path)
    paths.add_argument("--model-key", required=True)
    paths.add_argument("--task", choices=TASKS, required=True)
    paths.add_argument("--structure-index", type=int, required=True)
    validate_frame = sub.add_parser("validate-frame")
    validate_frame.add_argument("benchmark_root", type=Path)
    validate_frame.add_argument("--model-key", required=True)
    validate_frame.add_argument("--task", choices=TASKS, required=True)
    validate_frame.add_argument("--structure-index", type=int, required=True)
    beat = sub.add_parser("heartbeat")
    beat.add_argument("benchmark_root", type=Path)
    beat.add_argument("--owner-id", required=True)
    release = sub.add_parser("release-lease")
    release.add_argument("benchmark_root", type=Path)
    release.add_argument("--owner-id", required=True)
    finalize = sub.add_parser("finalize-run")
    finalize.add_argument("benchmark_root", type=Path)
    finalize.add_argument("--owner-id", required=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "init-or-resume":
            result = init_or_resume(args)
            code = 2 if result.get("action") == "selection_required" else 0
        elif args.command == "initialize-frames":
            result = initialize_frames(args.benchmark_root.resolve(), args.owner_id, args.frame_count, args.observed_adsorbate)
            code = 0
        elif args.command == "reconcile":
            result = reconcile_run(args.benchmark_root.resolve(), args.owner_id, external_jobs_reconciled=args.external_jobs_reconciled)
            code = 0
        elif args.command == "record-frame":
            result = record_frame(args.benchmark_root.resolve(), args.owner_id, args.model_key, args.task, args.structure_index, args.outcome)
            code = 0
        elif args.command == "frame-paths":
            result = {key: str(value.relative_to(args.benchmark_root.resolve())).replace("\\", "/") for key, value in frame_paths(args.benchmark_root.resolve(), args.model_key, args.task, args.structure_index).items()}
            code = 0
        elif args.command == "validate-frame":
            result = validate_frame_artifact(args.benchmark_root.resolve(), args.model_key, args.task, args.structure_index)
            code = 0 if result["valid"] else 1
        elif args.command == "heartbeat":
            result = heartbeat(args.benchmark_root.resolve(), args.owner_id)
            code = 0
        elif args.command == "release-lease":
            result = release_lease(args.benchmark_root.resolve(), args.owner_id)
            code = 0
        else:
            result = finalize_run(args.benchmark_root.resolve(), args.owner_id)
            code = 0
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return code
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
