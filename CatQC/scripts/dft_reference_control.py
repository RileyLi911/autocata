from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shlex
import sys
import xml.etree.ElementTree as ET
import threading
from pathlib import Path
from typing import Any

from common import (
    as_float, atomic_write_csv, atomic_write_json, canonical_json_sha256, load_json,
    read_csv, read_structure_frames, sha256_file,
)
from run_control import SCHEMA_VERSION, append_event, heartbeat, iso_time, load_core, require_owner


SYSTEM_TYPES = ("slab_adsorbate", "clean_slab", "isolated_adsorbate")
SECRET_KEY_RE = re.compile(r"password|passwd|token|secret|private.?key|api.?key|ssh.?key|access.?key|credential", re.I)
SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")


class DFTControlError(RuntimeError):
    pass


def relative(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def reject_secrets(value: Any, path: str = "execution_profile") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) != "credentials_policy" and SECRET_KEY_RE.search(str(key)):
                raise DFTControlError(f"{path} contains forbidden credential field {key!r}")
            reject_secrets(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_secrets(item, f"{path}[{index}]")


def validate_profile(profile: dict[str, Any]) -> None:
    if profile.get("schema_version") != SCHEMA_VERSION:
        raise DFTControlError("VASP profile schema_version must be 4")
    if profile.get("confirmed") is not True or profile.get("confirmed_by") != "user":
        raise DFTControlError("VASP profile must be explicitly confirmed by the user")
    for field in ("profile_id", "confirmed_at", "incar", "kpoints", "energy_source", "output_validation", "relaxation", "isolated_adsorbate", "potcar_generation"):
        if field not in profile:
            raise DFTControlError(f"VASP profile is missing {field}")
    if profile["energy_source"] not in {"energy_sigma_to_zero", "free_energy_toten"}:
        raise DFTControlError("unsupported VASP energy_source")
    if float(profile["output_validation"].get("energy_tolerance_eV", 0)) <= 0:
        raise DFTControlError("output_validation.energy_tolerance_eV must be positive")
    relaxation = profile["relaxation"]
    if relaxation.get("fixed_cell") is not True or float(relaxation.get("force_threshold_eV_per_A", 0)) <= 0 or int(relaxation.get("max_ionic_steps", 0)) < 1:
        raise DFTControlError("DFT relaxation requires a fixed cell and positive force threshold")
    if relaxation.get("slab_constraint_rule") != "fix_bottom_1_layer_for_n_layers_lte_5_else_bottom_2_layers":
        raise DFTControlError("DFT slab constraint rule must match the benchmark baseline")
    isolated = profile["isolated_adsorbate"]
    if isolated.get("gamma_only") is not True or isolated.get("pbc") is not True or float(isolated.get("vacuum_A", 0)) <= 0:
        raise DFTControlError("isolated adsorbate requires confirmed periodic gamma-only vacuum settings")
    if "charge" not in isolated or "spin_multiplicity" not in isolated:
        raise DFTControlError("isolated adsorbate charge and spin multiplicity must be explicit")
    from dft_profile import validate_selection
    try:
        validate_selection(profile)
    except (ValueError, KeyError, TypeError) as exc:
        raise DFTControlError(str(exc)) from exc
    potcar = profile["potcar_generation"]
    if potcar.get("backend") not in {"vaspkit", "direct"}:
        raise DFTControlError("POTCAR backend must be vaspkit or direct")
    labels = potcar.get("element_labels") or {}
    if not labels or any(not SAFE_LABEL_RE.fullmatch(str(label)) for label in labels.values()):
        raise DFTControlError("POTCAR element labels are missing or unsafe")
    if potcar["backend"] == "direct" and not str(potcar.get("potcar_root") or "").strip():
        raise DFTControlError("direct POTCAR generation requires potcar_root")
    if potcar["backend"] == "vaspkit":
        for field in ("vaspkit_executable", "vaspkit_task", "validated_vaspkit_version", "vaspkit_version_command"):
            if not potcar.get(field):
                raise DFTControlError(f"VASPKIT POTCAR generation requires {field}")
        if "vaspkit_input_lines" not in potcar or not isinstance(potcar["vaspkit_input_lines"], list):
            raise DFTControlError("VASPKIT POTCAR generation requires an explicitly confirmed vaspkit_input_lines array")


def validate_execution_profile(profile: dict[str, Any]) -> None:
    reject_secrets(profile)
    if profile.get("schema_version") != SCHEMA_VERSION or profile.get("backend") != "dpdispatcher":
        raise DFTControlError("execution profile must be a v4 DPDispatcher profile")
    if profile.get("credentials_policy") != "external_only":
        raise DFTControlError("credentials_policy must be external_only")
    for field in ("machine", "resources", "vasp_command", "forward_files", "backward_files"):
        if not profile.get(field):
            raise DFTControlError(f"execution profile is missing {field}")
    preflight = profile.get("runtime_preflight") or {}
    required_preflight = ("stack_limit_policy", "apply_in_submission_shell", "apply_in_job_shell", "verify_after_launch", "mpi_launcher", "mpi_version_command", "vasp_version_command")
    for field in required_preflight:
        if field not in preflight or (isinstance(preflight[field], str) and not preflight[field].strip()):
            raise DFTControlError(f"runtime_preflight is missing {field}")
    if preflight["stack_limit_policy"] not in {"required_unlimited", "probe_then_confirm", "unchanged"}:
        raise DFTControlError("runtime_preflight.stack_limit_policy is invalid")
    if preflight["stack_limit_policy"] == "required_unlimited" and (preflight["apply_in_submission_shell"] is not True or preflight["apply_in_job_shell"] is not True or preflight["verify_after_launch"] is not True):
        raise DFTControlError("required_unlimited stack policy must apply and verify in both shells")
    resource_policy = profile.get("resource_policy") or {}
    for field in ("cpu_per_task", "max_concurrent_tasks", "oversubscription"):
        if field not in resource_policy:
            raise DFTControlError(f"resource_policy is missing {field}")
    if int(resource_policy["cpu_per_task"]) < 1 or int(resource_policy["max_concurrent_tasks"]) < 1 or resource_policy["oversubscription"] != "forbidden":
        raise DFTControlError("resource_policy must define positive CPU/concurrency limits and forbid oversubscription")
    timeout_policy = profile.get("timeout_policy") or {}
    required_timeout = ("dft_walltime", "dft_per_calculation_timeout", "dft_idle_timeout", "automatic_timeout_cancel")
    for field in required_timeout:
        if field not in timeout_policy:
            raise DFTControlError(f"timeout_policy is missing {field}")
    if timeout_policy.get("dft_walltime") != "none" or timeout_policy.get("dft_per_calculation_timeout") != "none" or timeout_policy.get("dft_idle_timeout") != "none" or timeout_policy.get("automatic_timeout_cancel") is not False:
        raise DFTControlError("DFT timeout policy must disable walltime, per-calculation timeout, idle timeout, and automatic timeout cancellation")
    output_monitor = profile.get("output_monitor") or {}
    if output_monitor.get("enabled") is not True or int(output_monitor.get("poll_interval_seconds") or 0) < 1:
        raise DFTControlError("output_monitor must be enabled with a positive polling interval")
    rules = output_monitor.get("termination_rules") or {}
    for field in ("fatal_process_error", "electronic_oscillation", "corrupted_output", "missing_output"):
        if field not in rules:
            raise DFTControlError(f"output_monitor.termination_rules is missing {field}")
    if rules["missing_output"] is not False or rules["electronic_oscillation"] not in {"disabled", "user_confirmed"}:
        raise DFTControlError("missing output cannot terminate DFT and oscillation must be disabled or user_confirmed")
    oscillation = output_monitor.get("electronic_oscillation") or {}
    for field in ("enabled", "window_steps", "minimum_observed_steps", "required_pattern", "action"):
        if field not in oscillation:
            raise DFTControlError(f"output_monitor.electronic_oscillation is missing {field}")
    if oscillation["action"] != "terminate_and_block":
        raise DFTControlError("electronic oscillation action must be terminate_and_block")
    forbidden_timeout = re.compile(r"(?:--time(?:=|\s)|--time-limit(?:=|\s)|\bwalltime\b|\btimeout\b)", re.I)
    for field in ("vasp_command",):
        if forbidden_timeout.search(str(profile.get(field) or "")):
            raise DFTControlError("DFT command contains a forbidden timeout or walltime setting")
    forward = {str(item).replace("\\", "/").split("/")[-1] for item in profile["forward_files"]}
    required_forward = {"POSCAR", "INCAR", "KPOINTS", "POTCAR.spec"}
    if not required_forward.issubset(forward):
        raise DFTControlError(f"forward_files must include {sorted(required_forward)}")
    forbidden_forward = {name for name in forward if name.upper().startswith("POTCAR") and name != "POTCAR.spec"}
    if forbidden_forward:
        raise DFTControlError("licensed POTCAR content must not be transported")
    backward = {str(item).replace("\\", "/").split("/")[-1] for item in profile["backward_files"]}
    required = {"vasprun.xml", "OUTCAR", "OSZICAR", "CONTCAR", "POTCAR.titel", "POTCAR.sha256"}
    if not required.issubset(backward):
        raise DFTControlError(f"backward_files must include {sorted(required)}")
    allowed_provenance = {"POTCAR.titel", "POTCAR.sha256"}
    forbidden_backward = {name for name in backward if name.upper().startswith("POTCAR") and name not in allowed_provenance}
    if forbidden_backward:
        raise DFTControlError("licensed POTCAR content must not be returned or archived")


def validate_provided(root: Path, owner_id: str) -> dict[str, Any]:
    require_owner(root, owner_id)
    manifest, _, state = load_core(root)
    if manifest["request"].get("dft_reference_mode", "provided") != "provided":
        raise DFTControlError("provided-table validation requires dft_reference_mode=provided")
    config = load_json(root / "benchmark_config.json")
    configured = config.get("dft_reference_path")
    if not configured:
        raise DFTControlError("benchmark_config.dft_reference_path is required")
    source = Path(str(configured))
    if not source.is_absolute():
        source = root / source
    source = source.resolve()
    if not source.is_file():
        raise DFTControlError(f"DFT reference table does not exist: {source}")
    rows = read_csv(source)
    expected = int(config["frame_count"])
    indices: list[int] = []
    for row_number, row in enumerate(rows, start=2):
        try:
            index = int(row.get("structure_index", ""))
        except (TypeError, ValueError) as exc:
            raise DFTControlError(f"invalid structure_index at row {row_number}") from exc
        if as_float(row.get("dft_adsorption_energy_eV")) is None:
            raise DFTControlError(f"nonfinite dft_adsorption_energy_eV at structure_index {index}")
        indices.append(index)
    if indices != list(range(expected)):
        raise DFTControlError(f"provided DFT reference indices must be exactly 0..{expected - 1} in order")
    fieldnames = list(rows[0]) if rows else ["structure_index", "dft_adsorption_energy_eV"]
    target = root / "summary" / "dft_reference.csv"
    atomic_write_csv(target, fieldnames, rows)
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "run_id": manifest["run_id"],
        "validated_at": iso_time(),
        "source_path": str(source),
        "source_sha256": sha256_file(source),
        "copied_path": "summary/dft_reference.csv",
        "copied_sha256": sha256_file(target),
        "frame_count": expected,
        "valid_frames": len(rows),
    }
    evidence_path = root / "dft" / "provided_reference_manifest.json"
    atomic_write_json(evidence_path, evidence)
    state["dft_reference"].update({
        "status": "provided_ready", "expected_calculations": None,
        "completed_calculations": 0, "failed_calculations": 0, "active_calculations": 0,
        "valid_frames": len(rows), "coverage_fraction": 1.0,
        "manifest_path": "dft/provided_reference_manifest.json", "profile_sha256": None,
        "partial_approval_path": None, "next_action": "run_dft_and_mlp_branches",
        "last_updated": iso_time(),
    })
    append_event(root, state, owner_id, "provided_dft_reference_validated", frame_count=expected, source_sha256=evidence["source_sha256"])
    state["last_updated"] = iso_time()
    atomic_write_json(root / "run_state.json", state)
    return evidence


def unique_symbols(symbols: list[str]) -> list[str]:
    result: list[str] = []
    for symbol in symbols:
        if symbol not in result:
            result.append(symbol)
    return result


def failure_snapshot(root: Path, dft_manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    records: list[dict[str, Any]] = []
    for item in dft_manifest.get("calculations") or []:
        checkpoint = load_json(root / item["checkpoint_path"])
        if checkpoint.get("status") == "failed":
            records.append({
                "calculation_id": item["calculation_id"],
                "frame_indices": sorted(item.get("frame_indices") or []),
                "attempt_count": int(checkpoint.get("attempt_count") or 0),
                "failure_class": checkpoint.get("failure_class"),
                "last_error": checkpoint.get("last_error"),
            })
    records.sort(key=lambda item: item["calculation_id"])
    return records, canonical_json_sha256(records)


SCHEDULER_STATES = {"queued", "running", "completed", "failed", "cancelled", "preempted", "out_of_memory", "timeout", "unknown"}

FAILURE_CLASSES = {
    "soft_stack_sigsegv", "mpi_launch_failure", "scheduler_rejection",
    "resource_unavailable", "transport_failure", "potcar_failure",
    "missing_output", "truncated_output", "electronic_nonconvergence",
    "ionic_nonconvergence", "energy_crosscheck_failure", "scheduler_external_timeout", "output_diagnostic_stop", "unknown",
}


def classify_failure(output_root: Path, job: dict[str, Any] | None = None, error: str | None = None) -> tuple[str, list[str]]:
    fragments = [str(error or "")]
    for name in ("stdout", "stderr", "OUTCAR", "OSZICAR", "vasprun.xml"):
        path = output_root / name
        if path.is_file():
            fragments.append(path.read_text(encoding="utf-8", errors="replace")[-20000:])
    text = "\n".join(fragments).lower()
    patterns = [
        ("soft_stack_sigsegv", ("sigsegv", "segmentation fault", "stack limit", "soft stack")),
        ("mpi_launch_failure", ("mpi_abort", "mpirun", "mpi_init", "unable to launch")),
        ("scheduler_rejection", ("sbatch: error", "invalid qos", "invalid partition", "job rejected")),
        ("resource_unavailable", ("insufficient node", "resources", "out of memory", "oom")),
        ("potcar_failure", ("potcar", "titel order", "pseudopotential")),
        ("transport_failure", ("scp", "rsync", "connection reset", "transport")),
        ("electronic_nonconvergence", ("ediff", "electronic convergence")),
        ("ionic_nonconvergence", ("not converged", "ionic convergence", "reached required accuracy")),
        ("truncated_output", ("parse error", "truncated", "unexpected end")),
    ]
    for failure_class, needles in patterns:
        if any(needle in text for needle in needles):
            return failure_class, [needle for needle in needles if needle in text]
    if job and str(job.get("state") or "").lower() in {"timeout", "external_timeout"}:
        return "scheduler_external_timeout", ["scheduler_timeout"]
    if job and str(job.get("state") or "").lower() in {"failed", "cancelled", "preempted", "out_of_memory"}:
        return "scheduler_rejection", [str(job.get("state"))]
    if not (output_root / "vasprun.xml").is_file():
        return "missing_output", ["vasprun.xml"]
    return "unknown", []


def record_failure(root: Path, checkpoint: dict[str, Any], output_root: Path, job: dict[str, Any] | None, error: str) -> dict[str, Any]:
    failure_class, matched = classify_failure(output_root, job, error)
    calc_root = output_root.parent
    diagnostic = {
        "schema_version": SCHEMA_VERSION, "calculation_id": checkpoint["calculation_id"],
        "failure_class": failure_class, "matched_patterns": matched, "error": error,
        "observed_at": iso_time(), "job_state": (job or {}).get("state"),
        "exit_code": (job or {}).get("exit_code"), "signal": (job or {}).get("signal"),
        "files": {name: {"exists": (output_root / name).is_file(), "sha256": sha256_file(output_root / name) if (output_root / name).is_file() else None}
                  for name in ("vasprun.xml", "OUTCAR", "OSZICAR", "stdout", "stderr")},
    }
    diagnostic_path = calc_root / "errors" / "diagnostics.json"
    atomic_write_json(diagnostic_path, diagnostic)
    checkpoint.update({"failure_class": failure_class, "diagnostic_path": relative(diagnostic_path, root), "last_error": error, "exit_code": (job or {}).get("exit_code"), "signal": (job or {}).get("signal")})
    return diagnostic


def inspect_output_diagnostics(calc_root: Path, execution: dict[str, Any], job: dict[str, Any] | None) -> dict[str, Any]:
    output_root = calc_root / "outputs"
    monitor = execution["output_monitor"]
    rules = monitor["termination_rules"]
    oscillation = monitor["electronic_oscillation"]
    fragments: list[str] = []
    files: dict[str, dict[str, Any]] = {}
    for name in ("OUTCAR", "OSZICAR", "vasprun.xml", "stdout", "stderr"):
        path = output_root / name
        exists = path.is_file()
        files[name] = {"exists": exists, "size_bytes": path.stat().st_size if exists else None, "sha256": sha256_file(path) if exists else None}
        if exists:
            fragments.append(path.read_text(encoding="utf-8", errors="replace")[-40000:])
    text = "\n".join(fragments)
    lower = text.lower()
    matched: list[str] = []
    if rules["fatal_process_error"] and any(pattern in lower for pattern in ("sigsegv", "segmentation fault", "mpi_abort", "mpi abort", "fatal error", "internal error")):
        matched.append("fatal_process_error")
    marker_patterns = oscillation.get("marker_patterns") or ["brmix", "eddav", "electronic step oscillation"]
    marker_hit = any(str(pattern).lower() in lower for pattern in marker_patterns)
    dav_steps = len(re.findall(r"^\s*dav:\s*\d+", text, flags=re.I | re.M))
    oscillation_hit = bool(oscillation["enabled"] and rules["electronic_oscillation"] == "user_confirmed" and marker_hit and dav_steps >= int(oscillation["minimum_observed_steps"]))
    if oscillation_hit:
        matched.append("electronic_oscillation")
    scheduler_state = str((job or {}).get("state") or "").lower()
    external_scheduler = scheduler_state in {"failed", "cancelled", "preempted", "out_of_memory", "timeout"}
    if external_scheduler:
        matched.append("scheduler_external_termination")
    termination_allowed = bool(matched)
    reason = "termination evidence detected" if termination_allowed else "no termination evidence; long runtime or missing output is not a failure"
    diagnostic = {
        "schema_version": SCHEMA_VERSION, "calculation_id": calc_root.name, "observed_at": iso_time(),
        "termination_allowed": termination_allowed, "termination_requested": termination_allowed,
        "reason": reason, "matched_rules": matched,
        "electronic_steps": [{"dav_steps": dav_steps, "marker_hit": marker_hit, "minimum_observed_steps": oscillation["minimum_observed_steps"]}],
        "files": files,
        "warnings": [] if termination_allowed else (["possible_electronic_oscillation"] if marker_hit else []),
        "job_state": scheduler_state or None,
    }
    return diagnostic


def monitor_outputs(root: Path, owner_id: str) -> dict[str, Any]:
    require_owner(root, owner_id)
    manifest, _, state = load_core(root)
    dft_manifest = load_json(root / "dft" / "dft_manifest.json")
    execution = load_json(root / dft_manifest["execution_profile_path"])
    validate_execution_profile(execution)
    dft_state_path = root / "dft" / "state" / "dft_state.json"
    dft_state = load_json(dft_state_path)
    events: list[dict[str, Any]] = []
    diagnostic_stops = 0
    for item in dft_manifest["calculations"]:
        checkpoint_path = root / item["checkpoint_path"]
        checkpoint = load_json(checkpoint_path)
        calc_root = checkpoint_path.parent
        job_path = root / checkpoint["job_status_path"]
        job = load_json(job_path) if job_path.is_file() else None
        diagnostic = inspect_output_diagnostics(calc_root, execution, job)
        diagnostic_path = calc_root / "errors" / "output_diagnostics.json"
        atomic_write_json(diagnostic_path, diagnostic)
        if diagnostic["termination_requested"]:
            diagnostic_stops += 1
            termination_event = {"calculation_id": item["calculation_id"], "source": "output_diagnostic" if "scheduler_external_termination" not in diagnostic["matched_rules"] else "scheduler", "reason": diagnostic["reason"], "matched_rules": diagnostic["matched_rules"], "requested_at": iso_time()}
            events.append(termination_event)
            atomic_write_json(calc_root / "errors" / "termination_request.json", termination_event)
            checkpoint.update({"status": "blocked", "termination_reason": diagnostic["reason"], "termination_source": termination_event["source"], "diagnostic_path": relative(diagnostic_path, root), "recovery_action": "user_decision_or_same_profile_recovery"})
            if job:
                job.update({"state": "output_diagnostic_stop" if termination_event["source"] == "output_diagnostic" else "external_timeout" if "timeout" in diagnostic["matched_rules"] else "scheduler_terminal", "termination_reason": diagnostic["reason"], "termination_source": termination_event["source"], "diagnostic_path": relative(diagnostic_path, root)})
                atomic_write_json(job_path, job)
        else:
            checkpoint["diagnostic_path"] = relative(diagnostic_path, root)
        checkpoint["updated_at"] = iso_time()
        atomic_write_json(checkpoint_path, checkpoint)
    dft_state.update({"termination_events": events, "diagnostic_stop_count": diagnostic_stops, "output_monitor_policy": execution["output_monitor"], "timeout_policy": execution["timeout_policy"], "updated_at": iso_time()})
    atomic_write_json(dft_state_path, dft_state)
    state["dft_reference"].update({"termination_events": events, "diagnostic_stop_count": diagnostic_stops, "last_updated": iso_time()})
    append_event(root, state, owner_id, "dft_output_monitor_checked", diagnostic_stops=diagnostic_stops)
    state["last_updated"] = iso_time()
    atomic_write_json(root / "run_state.json", state)
    return {"status": "checked", "diagnostic_stops": diagnostic_stops, "termination_events": events, "next_action": "reconcile_scheduler_before_collect"}


def validate_runtime_probe(probe: dict[str, Any], execution: dict[str, Any], run_id: str, requested_cpu: int, pending_tasks: int) -> dict[str, Any]:
    required = ("schema_version", "run_id", "passed", "submission_shell_stack", "job_shell_stack", "vasp_version", "mpi_launcher", "mpi_version", "cpu_available", "memory_bytes", "smoke_test")
    missing = [field for field in required if field not in probe]
    if missing:
        raise DFTControlError(f"runtime probe is missing {missing}")
    if probe.get("schema_version") != SCHEMA_VERSION or probe.get("run_id") != run_id:
        raise DFTControlError("runtime probe schema or run_id does not match the run")
    if probe.get("passed") is not True or (probe.get("smoke_test") or {}).get("passed") is not True:
        raise DFTControlError("runtime probe or representative smoke test did not pass")
    preflight = execution["runtime_preflight"]
    if preflight["stack_limit_policy"] == "required_unlimited":
        if probe.get("submission_shell_stack") != "unlimited" or probe.get("job_shell_stack") != "unlimited":
            raise DFTControlError("both submission and job shells must report unlimited stack")
    policy = execution["resource_policy"]
    if pending_tasks > int(policy["max_concurrent_tasks"]):
        concurrent = int(policy["max_concurrent_tasks"])
    else:
        concurrent = pending_tasks
    if int(probe.get("cpu_available") or 0) < requested_cpu:
        raise DFTControlError(f"available CPU ({probe.get('cpu_available')}) is below requested CPU ({requested_cpu})")
    return {
        "pending_tasks": pending_tasks, "requested_cpu": requested_cpu,
        "available_cpu": int(probe["cpu_available"]), "memory_bytes": int(probe["memory_bytes"]),
        "max_concurrent_tasks": int(policy["max_concurrent_tasks"]),
        "planned_concurrent_tasks": concurrent, "cpu_per_task": int(policy["cpu_per_task"]),
        "oversubscription": policy["oversubscription"],
    }


def run_preflight(root: Path, owner_id: str, runtime_probe_path: Path | None = None) -> dict[str, Any]:
    require_owner(root, owner_id)
    manifest, _, state = load_core(root)
    dft_manifest = load_json(root / "dft" / "dft_manifest.json")
    execution_path = root / dft_manifest["execution_profile_path"]
    execution = load_json(execution_path)
    validate_execution_profile(execution)
    probe_path = (runtime_probe_path or (root / "dft" / "runtime_probe.json")).resolve()
    if not probe_path.is_file():
        dft_state_path = root / "dft" / "state" / "dft_state.json"
        if dft_state_path.is_file():
            dft_state = load_json(dft_state_path)
            dft_state.update({"preflight_status": "failed", "next_recovery_action": "collect_runtime_probe", "updated_at": iso_time()})
            atomic_write_json(dft_state_path, dft_state)
        raise DFTControlError(f"runtime probe evidence is required: {probe_path}")
    probe = load_json(probe_path)
    pending = [item for item in dft_manifest["calculations"] if load_json(root / item["checkpoint_path"]).get("status") in {"pending", "failed"}]
    requested_cpu = min(len(pending), int(execution["resource_policy"]["max_concurrent_tasks"])) * int(execution["resource_policy"]["cpu_per_task"])
    try:
        allocation = validate_runtime_probe(probe, execution, manifest["run_id"], requested_cpu, len(pending))
    except DFTControlError as exc:
        dft_state_path = root / "dft" / "state" / "dft_state.json"
        dft_state = load_json(dft_state_path)
        dft_state.update({"preflight_status": "failed", "next_recovery_action": "repair_runtime_preflight", "updated_at": iso_time()})
        atomic_write_json(dft_state_path, dft_state)
        evidence = {"schema_version": SCHEMA_VERSION, "run_id": manifest["run_id"], "status": "failed", "error": str(exc), "execution_profile_sha256": sha256_file(execution_path), "runtime_probe_path": relative(probe_path, root), "runtime_probe_sha256": sha256_file(probe_path), "stack_policy": execution["runtime_preflight"], "resource_allocation": {}, "validated_at": iso_time()}
        atomic_write_json(root / "dft" / "execution_preflight.json", evidence)
        raise
    evidence = {
        "schema_version": SCHEMA_VERSION, "run_id": manifest["run_id"], "status": "passed",
        "execution_profile_sha256": sha256_file(execution_path), "runtime_probe_sha256": sha256_file(probe_path),
        "runtime_probe_path": relative(probe_path, root), "stack_policy": execution["runtime_preflight"],
        "resource_allocation": allocation, "probe": probe, "validated_at": iso_time(),
    }
    atomic_write_json(root / "dft" / "execution_preflight.json", evidence)
    dft_state_path = root / "dft" / "state" / "dft_state.json"
    dft_state = load_json(dft_state_path)
    dft_state.update({"preflight_status": "passed", "resource_allocation": allocation, "next_recovery_action": None, "updated_at": iso_time()})
    atomic_write_json(dft_state_path, dft_state)
    state["dft_reference"].update({"preflight_status": "passed", "resource_allocation": allocation, "last_updated": iso_time()})
    append_event(root, state, owner_id, "dft_execution_preflight_passed", requested_cpu=requested_cpu, available_cpu=allocation["available_cpu"])
    state["last_updated"] = iso_time()
    atomic_write_json(root / "run_state.json", state)
    return evidence


def load_scheduler_evidence(path: Path, run_id: str, calculation_ids: set[str]) -> dict[str, Any]:
    evidence = load_json(path)
    if evidence.get("schema_version") != SCHEMA_VERSION or evidence.get("run_id") != run_id:
        raise DFTControlError("scheduler evidence schema or run_id does not match the DFT run")
    if not evidence.get("queried_at") or not evidence.get("source"):
        raise DFTControlError("scheduler evidence requires queried_at and source")
    jobs = evidence.get("jobs")
    if not isinstance(jobs, list):
        raise DFTControlError("scheduler evidence jobs must be an array")
    seen: set[str] = set()
    for item in jobs:
        calculation_id = item.get("calculation_id")
        state = item.get("state")
        if calculation_id not in calculation_ids:
            raise DFTControlError(f"scheduler evidence contains unknown calculation {calculation_id!r}")
        if calculation_id in seen:
            raise DFTControlError(f"scheduler evidence contains duplicate calculation {calculation_id!r}")
        if state not in SCHEDULER_STATES:
            raise DFTControlError(f"unsupported scheduler state {state!r}")
        if state in {"queued", "running"} and item.get("job_id") is None and item.get("pid") is None:
            raise DFTControlError(f"active calculation {calculation_id} requires job_id or pid")
        seen.add(calculation_id)
    missing = calculation_ids - seen
    if missing:
        raise DFTControlError(f"scheduler evidence is incomplete; missing calculations: {sorted(missing)}")
    return evidence


def reconcile(root: Path, owner_id: str, evidence_path: Path) -> dict[str, Any]:
    require_owner(root, owner_id)
    manifest, _, state = load_core(root)
    if not state.get("reconciliation_required") and not state.get("active_jobs"):
        raise DFTControlError("DFT reconciliation is not required and no active job is recorded")
    dft_manifest = load_json(root / "dft" / "dft_manifest.json")
    calculation_ids = {item["calculation_id"] for item in dft_manifest["calculations"]}
    evidence_path = evidence_path.resolve()
    evidence = load_scheduler_evidence(evidence_path, manifest["run_id"], calculation_ids)
    evidence_sha = sha256_file(evidence_path)
    by_id = {item["calculation_id"]: item for item in evidence["jobs"]}
    changed: list[dict[str, Any]] = []
    unknown: list[str] = []
    active_jobs: list[dict[str, Any]] = []
    for item in dft_manifest["calculations"]:
        calculation_id = item["calculation_id"]
        update = by_id[calculation_id]
        checkpoint_path = root / item["checkpoint_path"]
        checkpoint = load_json(checkpoint_path)
        job_path = root / checkpoint["job_status_path"]
        job = load_json(job_path)
        scheduler_state = update["state"]
        job.update({
            "state": scheduler_state,
            "job_id": update.get("job_id"),
            "pid": update.get("pid"),
            "last_checked": iso_time(),
            "scheduler_evidence": {
                "source": evidence["source"], "queried_at": evidence["queried_at"],
                "scheduler_state": update.get("scheduler_state"),
                "message": update.get("message"), "evidence_sha256": evidence_sha,
            },
        })
        atomic_write_json(job_path, job)
        if scheduler_state in {"queued", "running"}:
            checkpoint["status"] = scheduler_state
            active_jobs.append({"calculation_id": calculation_id, "job_id": update.get("job_id"), "pid": update.get("pid"), "state": scheduler_state})
        elif scheduler_state == "unknown":
            checkpoint["status"] = "blocked"
            checkpoint["last_error"] = update.get("message") or "scheduler returned unknown state; manual reconciliation required"
            unknown.append(calculation_id)
        elif scheduler_state in {"failed", "cancelled", "preempted", "out_of_memory", "timeout"}:
            checkpoint["status"] = "blocked" if scheduler_state == "timeout" else "failed"
            checkpoint["failure_class"] = "scheduler_external_timeout" if scheduler_state == "timeout" else "scheduler_rejection"
            checkpoint["external_scheduler_state"] = scheduler_state
            checkpoint["termination_source"] = "scheduler"
            checkpoint["last_error"] = update.get("message") or f"external scheduler terminal state: {scheduler_state}"
            if scheduler_state == "timeout":
                checkpoint["termination_reason"] = "scheduler_external_timeout"
            diagnostic_path = root / checkpoint["output_directory"] / ".." / "errors" / "output_diagnostics.json"
            checkpoint["diagnostic_path"] = relative(diagnostic_path.resolve(), root)
        # completed is only scheduler evidence; collect still requires valid output.
        checkpoint["updated_at"] = iso_time()
        atomic_write_json(checkpoint_path, checkpoint)
        changed.append({"calculation_id": calculation_id, "scheduler_state": scheduler_state})
    state["reconciliation_required"] = bool(unknown)
    state["active_jobs"] = active_jobs
    state["submission_allowed"] = not active_jobs and not unknown
    state["next_action"] = "manual_scheduler_reconciliation" if unknown else ("collect_dft_outputs" if not active_jobs else "monitor_active_dft_jobs")
    state["last_updated"] = iso_time()
    append_event(root, state, owner_id, "dft_scheduler_reconciled", source=evidence["source"], active=len(active_jobs), unknown=len(unknown))
    atomic_write_json(root / "run_state.json", state)
    result = {
        "schema_version": SCHEMA_VERSION, "run_id": manifest["run_id"], "evidence_sha256": evidence_sha,
        "active_jobs": active_jobs, "unknown_calculations": unknown, "changes": changed,
        "submission_allowed": state["submission_allowed"], "next_action": state["next_action"],
    }
    reconciliation_root = root / "dft" / "reconciliation"
    reconciliation_root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(reconciliation_root / "latest.json", result)
    return result


def structure_record(atoms: Any, fixed_indices: list[int], system_type: str, profile_sha256: str) -> dict[str, Any]:
    return {
        "system_type": system_type,
        "symbols": atoms.get_chemical_symbols(),
        "cell_A": [[round(float(value), 10) for value in row] for row in atoms.cell.array],
        "pbc": [bool(value) for value in atoms.pbc],
        "positions_A": [[round(float(value), 10) for value in row] for row in atoms.positions],
        "fixed_indices": sorted(fixed_indices),
        "profile_sha256": profile_sha256,
    }


def layer_fixed_indices(atoms: Any, slab_indices: list[int], tolerance: float) -> list[int]:
    ordered = sorted((float(atoms.positions[index][2]), index) for index in slab_indices)
    layers: list[list[int]] = []
    centers: list[float] = []
    for z_value, index in ordered:
        if not layers or abs(z_value - centers[-1]) > tolerance:
            layers.append([index])
            centers.append(z_value)
        else:
            layers[-1].append(index)
            centers[-1] = sum(float(atoms.positions[item][2]) for item in layers[-1]) / len(layers[-1])
    fixed_layer_count = 1 if len(layers) <= 5 else 2
    return sorted(index for layer in layers[:fixed_layer_count] for index in layer)


def unwrap_isolated_adsorbate(frame: Any, adsorbate_indices: list[int], vacuum_A: float) -> Any:
    try:
        from ase.geometry import find_mic
    except ImportError as exc:
        raise DFTControlError("ASE is required for DFT structure preparation") from exc
    adsorbate = frame[adsorbate_indices].copy()
    original = frame.positions[adsorbate_indices]
    anchor = original[0]
    rebuilt = [anchor]
    for position in original[1:]:
        vector, _ = find_mic(position - anchor, frame.cell, frame.pbc)
        rebuilt.append(anchor + vector)
    adsorbate.positions = rebuilt
    adsorbate.set_cell([1.0, 1.0, 1.0])
    adsorbate.set_pbc(True)
    adsorbate.center(vacuum=vacuum_A)
    return adsorbate


def write_incar(path: Path, incar: dict[str, Any]) -> None:
    lines = []
    for key in sorted(incar):
        value = incar[key]
        if isinstance(value, bool):
            value = ".TRUE." if value else ".FALSE."
        elif isinstance(value, list):
            value = " ".join(str(item) for item in value)
        lines.append(f"{str(key).upper()} = {value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_kpoints(path: Path, config: dict[str, Any], system_type: str) -> None:
    mode = config.get("mode")
    if system_type == "isolated_adsorbate" or mode == "gamma_only":
        lines = ["Gamma-only", "0", "Gamma", "1 1 1", "0 0 0"]
    elif mode == "explicit_grid":
        grid = config.get("grid") or []
        shift = config.get("shift") or [0, 0, 0]
        if len(grid) != 3 or len(shift) != 3:
            raise DFTControlError("explicit_grid KPOINTS requires three grid and shift values")
        lines = ["Explicit grid", "0", "Gamma", " ".join(map(str, grid)), " ".join(map(str, shift))]
    elif mode == "literal" and config.get("literal_lines"):
        lines = list(config["literal_lines"])
    else:
        raise DFTControlError("invalid KPOINTS configuration")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def potcar_build_record(profile: dict[str, Any], symbols: list[str]) -> dict[str, Any]:
    config = profile["potcar_generation"]
    ordered = unique_symbols(symbols)
    try:
        labels = [config["element_labels"][symbol] for symbol in ordered]
    except KeyError as exc:
        raise DFTControlError(f"POTCAR label is missing for element {exc.args[0]}") from exc
    if config["backend"] == "direct":
        root = str(config["potcar_root"]).rstrip("/\\")
        sources = [f"{root}/{label}/POTCAR" for label in labels]
        command = "cat " + " ".join(shlex.quote(item) for item in sources) + " > POTCAR"
    else:
        executable = shlex.quote(str(config["vaspkit_executable"]))
        task = int(config["vaspkit_task"])
        input_lines = config.get("vaspkit_input_lines") or []
        if input_lines:
            quoted_lines = " ".join(shlex.quote(str(item)) for item in input_lines)
            command = f"printf '%s\\n' {quoted_lines} | {executable} -task {task}"
        else:
            command = f"{executable} -task {task}"
        expected_version = str(config["validated_vaspkit_version"])
        version_command = str(config["vaspkit_version_command"])
        command = f"{version_command} > VASPKIT.version 2>&1 && grep -F -- {shlex.quote(expected_version)} VASPKIT.version && {command}"
    return {
        "backend": config["backend"],
        "functional_family": config["functional_family"],
        "element_order": ordered,
        "element_labels": labels,
        "command": command,
        "validated_vaspkit_version": config.get("validated_vaspkit_version"),
        "vaspkit_version_command": config.get("vaspkit_version_command"),
    }


def prepare(root: Path, owner_id: str, profile_path: Path, execution_profile_path: Path) -> dict[str, Any]:
    require_owner(root, owner_id)
    manifest, _, state = load_core(root)
    if manifest["request"].get("dft_reference_mode", "provided") != "generate_vasp":
        raise DFTControlError("DFT preparation requires dft_reference_mode=generate_vasp")
    if (root / "dft" / "dft_manifest.json").exists():
        raise DFTControlError("DFT manifest already exists and is immutable; resume with submit/collect or create a new run")
    config = load_json(root / "benchmark_config.json")
    profile = load_json(profile_path.resolve())
    execution = load_json(execution_profile_path.resolve())
    validate_profile(profile)
    validate_execution_profile(execution)
    dft_config = config.get("dft_reference") or {}
    derivation = dft_config.get("structure_derivation") or {}
    adsorbate_indices = sorted(int(index) for index in derivation.get("adsorbate_indices") or [])
    if not adsorbate_indices:
        raise DFTControlError("DFT structure derivation requires confirmed adsorbate_indices")
    structure_path = Path(config["structure_path"])
    if not structure_path.is_absolute():
        structure_path = root / structure_path
    structure_path = structure_path.resolve()
    try:
        from ase.constraints import FixAtoms
        from ase.io import write
    except ImportError as exc:
        raise DFTControlError("ASE is required; run prepare with a recorded ASE-capable environment") from exc
    try:
        frames, source_provenance = read_structure_frames(structure_path)
    except (FileNotFoundError, ImportError, RuntimeError, ValueError) as exc:
        raise DFTControlError(f"unable to read structure input {structure_path}: {exc}") from exc
    if len(frames) != int(config["frame_count"]):
        raise DFTControlError("structure frame count does not match benchmark_config")
    if any(index >= len(frame) for frame in frames for index in adsorbate_indices):
        raise DFTControlError("an adsorbate index is outside at least one structure frame")
    dft_root = root / "dft"
    calculations_root = dft_root / "calculations"
    calculations_root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(dft_root / "vasp_profile.json", profile)
    atomic_write_json(dft_root / "execution_profile.json", execution)
    profile_sha = sha256_file(dft_root / "vasp_profile.json")
    execution_sha = sha256_file(dft_root / "execution_profile.json")
    tolerance = float(profile["relaxation"]["layer_tolerance_A"])
    vacuum = float(profile["isolated_adsorbate"]["vacuum_A"])
    calculations: dict[str, dict[str, Any]] = {}

    def register(atoms: Any, system_type: str, fixed_indices: list[int], frame_index: int) -> str:
        record = structure_record(atoms, fixed_indices, system_type, profile_sha)
        digest = canonical_json_sha256(record)
        calculation_id = f"{system_type}-{digest[:16]}"
        if calculation_id in calculations:
            if frame_index not in calculations[calculation_id]["frame_indices"]:
                calculations[calculation_id]["frame_indices"].append(frame_index)
            return calculation_id
        calc_root = calculations_root / calculation_id
        inputs = calc_root / "inputs"
        outputs = calc_root / "outputs"
        errors = calc_root / "errors"
        for path in (inputs, outputs, errors):
            path.mkdir(parents=True, exist_ok=True)
        if fixed_indices:
            atoms.set_constraint(FixAtoms(indices=fixed_indices))
        write(str(inputs / "POSCAR"), atoms, format="vasp", direct=True, vasp5=True, sort=False)
        from dft_profile import effective_incar
        try:
            actual_incar = effective_incar(profile, system_type, atoms.get_chemical_symbols())
        except (ValueError, KeyError) as exc:
            raise DFTControlError(str(exc)) from exc
        write_incar(inputs / "INCAR", actual_incar)
        write_kpoints(inputs / "KPOINTS", profile["kpoints"], system_type)
        potcar = potcar_build_record(profile, atoms.get_chemical_symbols())
        (inputs / "POTCAR.spec").write_text("\n".join(potcar["element_labels"]) + "\n", encoding="utf-8")
        input_manifest = {
            "schema_version": SCHEMA_VERSION,
            "run_id": manifest["run_id"],
            "calculation_id": calculation_id,
            "system_type": system_type,
            "structure_sha256": digest,
            "profile_sha256": profile_sha,
            "fixed_indices": fixed_indices,
            "effective_incar": actual_incar,
            "potcar_build": potcar,
            "input_files": {
                name: {"size_bytes": (inputs / name).stat().st_size, "sha256": sha256_file(inputs / name)}
                for name in ("POSCAR", "INCAR", "KPOINTS", "POTCAR.spec")
            },
            "source_structure": {
                "input_path": source_provenance["input_path"],
                "input_mode": source_provenance["input_mode"],
                "source_files": source_provenance["source_files"],
                "structure_file_type": source_provenance["file_type"],
                "normalized_structure_sha256": digest,
                "poscar_sha256": sha256_file(inputs / "POSCAR"),
            },
        }
        atomic_write_json(calc_root / "input_manifest.json", input_manifest)
        checkpoint = {
            "schema_version": SCHEMA_VERSION,
            "run_id": manifest["run_id"],
            "calculation_id": calculation_id,
            "system_type": system_type,
            "structure_sha256": digest,
            "profile_sha256": profile_sha,
            "status": "pending",
            "attempt_count": 0,
            "frame_indices": [frame_index],
            "energy_eV": None,
            "electronic_converged": None,
            "ionic_converged": None,
            "input_manifest_path": relative(calc_root / "input_manifest.json", root),
            "job_status_path": relative(calc_root / "job_status.json", root),
            "output_directory": relative(outputs, root),
            "last_error": None,
            "failure_class": None,
            "diagnostic_path": None,
            "recovery_action": None,
            "exit_code": None,
            "signal": None,
            "wrapper_sha256": None,
            "termination_reason": None,
            "termination_source": None,
            "external_scheduler_state": None,
            "updated_at": iso_time(),
        }
        atomic_write_json(calc_root / "checkpoint.json", checkpoint)
        calculations[calculation_id] = {
            "calculation_id": calculation_id,
            "system_type": system_type,
            "structure_sha256": digest,
            "frame_indices": [frame_index],
            "checkpoint_path": relative(calc_root / "checkpoint.json", root),
        }
        return calculation_id

    isolated = unwrap_isolated_adsorbate(frames[0], adsorbate_indices, vacuum)
    isolated_id = register(isolated, "isolated_adsorbate", [], 0)
    frame_mapping: list[dict[str, Any]] = []
    for frame_index, original in enumerate(frames):
        frame = original.copy()
        slab_indices = [index for index in range(len(frame)) if index not in adsorbate_indices]
        fixed = layer_fixed_indices(frame, slab_indices, tolerance)
        slab_ads_id = register(frame, "slab_adsorbate", fixed, frame_index)
        clean = frame[slab_indices].copy()
        index_map = {old: new for new, old in enumerate(slab_indices)}
        clean_fixed = [index_map[index] for index in fixed]
        clean_id = register(clean, "clean_slab", clean_fixed, frame_index)
        if frame_index not in calculations[isolated_id]["frame_indices"]:
            calculations[isolated_id]["frame_indices"].append(frame_index)
        frame_mapping.append({
            "structure_index": frame_index,
            "slab_composition": clean.get_chemical_formula(),
            "slab_adsorbate_calculation_id": slab_ads_id,
            "clean_slab_calculation_id": clean_id,
            "isolated_adsorbate_calculation_id": isolated_id,
        })
    for item in calculations.values():
        item["frame_indices"].sort()
        checkpoint_path = root / item["checkpoint_path"]
        checkpoint = load_json(checkpoint_path)
        checkpoint["frame_indices"] = item["frame_indices"]
        atomic_write_json(checkpoint_path, checkpoint)
    dft_manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": manifest["run_id"],
        "created_at": iso_time(),
        "frame_count": len(frames),
        "structure_source": str(structure_path),
        "structure_input": source_provenance,
        "profile_path": "dft/vasp_profile.json",
        "profile_sha256": profile_sha,
        "execution_profile_path": "dft/execution_profile.json",
        "execution_profile_sha256": execution_sha,
        "calculations": sorted(calculations.values(), key=lambda item: item["calculation_id"]),
        "frame_mapping": frame_mapping,
    }
    atomic_write_json(dft_root / "dft_manifest.json", dft_manifest)
    manifest_sha = sha256_file(dft_root / "dft_manifest.json")
    dft_state = {
        "schema_version": SCHEMA_VERSION,
        "run_id": manifest["run_id"],
        "status": "prepared",
        "manifest_sha256": manifest_sha,
        "expected_calculations": len(calculations),
        "completed_calculations": 0,
        "failed_calculations": 0,
        "active_calculations": 0,
        "valid_frames": 0,
        "frame_count": len(frames),
        "coverage_fraction": 0.0,
        "active_jobs": [],
        "partial_approval_path": None,
        "next_action": "submit_pending_dft_calculations",
        "preflight_status": "pending",
        "failure_classes": {},
        "resource_allocation": {},
        "next_recovery_action": None,
        "timeout_policy": execution["timeout_policy"],
        "output_monitor_policy": execution["output_monitor"],
        "termination_events": [],
        "external_scheduler_limits": {},
        "diagnostic_stop_count": 0,
        "updated_at": iso_time(),
    }
    atomic_write_json(dft_root / "state" / "dft_state.json", dft_state)
    state["dft_reference"].update({
        "status": "prepared", "expected_calculations": len(calculations),
        "manifest_path": "dft/dft_manifest.json", "profile_sha256": profile_sha,
        "next_action": dft_state["next_action"], "last_updated": iso_time(),
    })
    append_event(root, state, owner_id, "dft_reference_prepared", calculations=len(calculations), frame_count=len(frames))
    state["last_updated"] = iso_time()
    atomic_write_json(root / "run_state.json", state)
    return dft_manifest


def build_submission(root: Path, owner_id: str) -> dict[str, Any]:
    require_owner(root, owner_id)
    manifest, _, state = load_core(root)
    if state.get("reconciliation_required"):
        raise DFTControlError("DFT submission is forbidden until external job reconciliation is complete")
    if state.get("adsorbate_validation") != "validated":
        raise DFTControlError("DFT submission requires validated Step 1 inputs and adsorbate")
    dft_summary = state.get("dft_reference") or {}
    if int(dft_summary.get("active_calculations") or 0) > 0:
        raise DFTControlError("active DFT calculations must be reconciled before another submission")
    dft_manifest = load_json(root / "dft" / "dft_manifest.json")
    execution = load_json(root / dft_manifest["execution_profile_path"])
    validate_execution_profile(execution)
    preflight_path = root / "dft" / "execution_preflight.json"
    if not preflight_path.is_file() or load_json(preflight_path).get("status") != "passed":
        raise DFTControlError("DFT execution preflight must pass before submission")
    preflight = load_json(preflight_path)
    if preflight.get("execution_profile_sha256") != sha256_file(root / dft_manifest["execution_profile_path"]):
        raise DFTControlError("execution preflight is stale relative to execution_profile.json")
    if sha256_file(root / dft_manifest["execution_profile_path"]) != dft_manifest["execution_profile_sha256"]:
        raise DFTControlError("DFT execution profile hash drift")
    if sha256_file(root / dft_manifest["profile_path"]) != dft_manifest["profile_sha256"]:
        raise DFTControlError("VASP profile hash drift")
    tasks = []
    for item in dft_manifest["calculations"]:
        checkpoint = load_json(root / item["checkpoint_path"])
        if checkpoint["status"] not in {"pending", "failed"}:
            continue
        calc_root = (root / item["checkpoint_path"]).parent
        input_manifest = load_json(root / checkpoint["input_manifest_path"])
        for name, evidence in input_manifest['input_files'].items():
            input_path = calc_root / 'inputs' / name
            if not input_path.is_file() or sha256_file(input_path) != evidence['sha256']:
                raise DFTControlError(f'DFT prepared input hash drift: {item["calculation_id"]}/{name}')
        provenance = "grep 'TITEL' POTCAR > POTCAR.titel && sha256sum POTCAR | awk '{print $1}' > POTCAR.sha256 && awk 'NR==FNR {want[++n]=$0; next} {m++; if (index($0,want[m])==0) exit 1} END {if (m != n) exit 1}' POTCAR.spec POTCAR.titel"
        staged_inputs = " ".join(f"inputs/{name}" for name in ("POSCAR", "INCAR", "KPOINTS", "POTCAR.spec"))
        wrapper_lines = ["set -euo pipefail"]
        stack_policy = execution["runtime_preflight"]["stack_limit_policy"]
        if stack_policy == "required_unlimited":
            wrapper_lines.extend(["stack_before=$(ulimit -s)", "ulimit -s unlimited", "stack_after=$(ulimit -s)", "test \"$stack_after\" = unlimited", "printf 'stack_limit_before=%s\\nstack_limit_after=%s\\n' \"$stack_before\" \"$stack_after\" > outputs/runtime_limits.txt"])
        wrapper_lines.extend([f"mkdir -p outputs", f"cp {staged_inputs} outputs/", "cd outputs", input_manifest["potcar_build"]["command"], provenance, execution["vasp_command"]])
        wrapper = "\n".join(wrapper_lines)
        wrapper_sha = hashlib.sha256(wrapper.encode("utf-8")).hexdigest()
        command = f"bash -lc {shlex.quote(wrapper)}"
        backward_files = [path if str(path).replace("\\", "/").startswith("outputs/") else f"outputs/{Path(path).name}" for path in execution["backward_files"]]
        if execution["runtime_preflight"]["verify_after_launch"] and "outputs/runtime_limits.txt" not in backward_files:
            backward_files.append("outputs/runtime_limits.txt")
        if input_manifest["potcar_build"]["backend"] == "vaspkit" and "outputs/VASPKIT.version" not in backward_files:
            backward_files.append("outputs/VASPKIT.version")
        tasks.append({
            "calculation_id": item["calculation_id"],
            "task_work_path": item["calculation_id"],
            "command": command,
            "wrapper": wrapper,
            "wrapper_sha256": wrapper_sha,
            "forward_files": execution["forward_files"],
            "backward_files": backward_files,
            "local_calculation_root": str(calc_root.resolve()),
        })
    descriptor = {
        "schema_version": SCHEMA_VERSION,
        "run_id": manifest["run_id"],
        "created_at": iso_time(),
        "machine": execution["machine"],
        "resources": execution["resources"],
        "runtime_preflight": execution["runtime_preflight"],
        "resource_policy": execution["resource_policy"],
        "preflight_sha256": sha256_file(preflight_path),
        "resource_allocation": preflight.get("resource_allocation"),
        "submission_shell_policy": execution["runtime_preflight"],
        "timeout_policy": execution["timeout_policy"],
        "output_monitor": execution["output_monitor"],
        "tasks": tasks,
    }
    reject_secrets(descriptor, "submission_descriptor")
    atomic_write_json(root / "dft" / "submission_descriptor.json", descriptor)
    return descriptor


def parse_vasprun(path: Path, profile: dict[str, Any]) -> dict[str, Any]:
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError) as exc:
        raise DFTControlError(f"unreadable vasprun.xml: {exc}") from exc
    calculations = tree.findall(".//calculation")
    if not calculations:
        raise DFTControlError("vasprun.xml contains no calculation")
    final = calculations[-1]
    energy_name = "e_0_energy" if profile["energy_source"] == "energy_sigma_to_zero" else "e_fr_energy"
    energy = None
    for item in final.findall("./energy/i"):
        if item.get("name") == energy_name:
            try:
                energy = float(item.text or "nan")
            except ValueError:
                energy = None
    if energy is None or not math.isfinite(energy):
        raise DFTControlError(f"final {energy_name} is missing or nonfinite")
    forces = []
    for vector in final.findall("./varray[@name='forces']/v"):
        try:
            values = [float(value) for value in (vector.text or "").split()]
        except ValueError:
            continue
        if len(values) == 3:
            forces.append(math.sqrt(sum(value * value for value in values)))
    if not forces:
        raise DFTControlError("final forces are missing from vasprun.xml")
    threshold = float(profile["relaxation"]["force_threshold_eV_per_A"])
    nelm = int(profile.get("incar", {}).get("NELM", 60))
    electronic_steps = len(final.findall("./scstep"))
    return {
        "energy_eV": energy,
        "max_force_eV_per_A": max(forces),
        "ionic_converged": max(forces) <= threshold,
        "electronic_converged": electronic_steps == 0 or electronic_steps < nelm,
        "electronic_steps_final_ionic_step": electronic_steps,
    }


def parse_crosscheck_energy(path: Path, profile: dict[str, Any]) -> float:
    """Read the independently written OUTCAR/OSZICAR energy evidence."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DFTControlError(f"unreadable {path.name}: {exc}") from exc
    if path.name.upper() == "OUTCAR":
        if profile["energy_source"] == "energy_sigma_to_zero":
            matches = re.findall(r"energy\(sigma->0\)\s*=\s*([-+0-9.Ee]+)", text)
        else:
            matches = re.findall(r"free\s+energy\s+TOTEN\s*=\s*([-+0-9.Ee]+)", text)
    else:
        matches = re.findall(r"(?:E0|F)=\s*([-+0-9.Ee]+)", text)
    if not matches:
        raise DFTControlError(f"{path.name} has no parseable final energy for {profile['energy_source']}")
    try:
        value = float(matches[-1])
    except ValueError as exc:
        raise DFTControlError(f"{path.name} final energy is not numeric") from exc
    if not math.isfinite(value):
        raise DFTControlError(f"{path.name} final energy is nonfinite")
    return value


def parse_outcar_convergence(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DFTControlError(f"unreadable OUTCAR: {exc}") from exc
    lower = text.lower()
    electronic_failed = bool(re.search(r"ediff\s+(?:was\s+)?not\s+reached|ediff\s+convergence\s+failed", lower))
    electronic_reached = "ediff is reached" in lower
    ionic_reached = "reached required accuracy - stopping structural energy minimisation" in lower
    ionic_failed = "reached required accuracy" not in lower and bool(
        re.search(r"aborting loop|convergence\s+(?:failed|not reached)", lower)
    )
    if electronic_failed or not electronic_reached:
        raise DFTControlError("OUTCAR does not provide a positive electronic-convergence marker")
    if ionic_failed or not ionic_reached:
        raise DFTControlError("OUTCAR does not provide a positive ionic-convergence marker")
    return {
        "electronic_converged": True,
        "ionic_converged": True,
        "electronic_marker": "EDIFF is reached",
        "ionic_marker": "reached required accuracy - stopping structural energy minimisation",
    }


def parse_oszicar_convergence(path: Path, profile: dict[str, Any]) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DFTControlError(f"unreadable OSZICAR: {exc}") from exc
    energy_lines = re.findall(r"^\s*\d+\s+.*(?:F=|E0=)\s*[-+0-9.Ee]+", text, flags=re.MULTILINE)
    if not energy_lines:
        raise DFTControlError("OSZICAR has no final ionic-step energy record")
    if re.search(r"(?:warning|error|not converged|convergence failed)", text, flags=re.IGNORECASE):
        raise DFTControlError("OSZICAR contains a convergence warning or error")
    return {"final_ionic_step_record": True, "ionic_step_records": len(energy_lines)}


def validate_energy_crosschecks(vasprun_energy: float, output_root: Path, profile: dict[str, Any]) -> dict[str, Any]:
    outcar = parse_crosscheck_energy(output_root / "OUTCAR", profile)
    oszicar = parse_crosscheck_energy(output_root / "OSZICAR", profile)
    outcar_convergence = parse_outcar_convergence(output_root / "OUTCAR")
    oszicar_convergence = parse_oszicar_convergence(output_root / "OSZICAR", profile)
    tolerance = float(profile["output_validation"]["energy_tolerance_eV"])
    if abs(vasprun_energy - outcar) > tolerance or abs(vasprun_energy - oszicar) > tolerance or abs(outcar - oszicar) > tolerance:
        raise DFTControlError(
            f"energy cross-check mismatch exceeds {tolerance:g} eV: "
            f"vasprun={vasprun_energy:g}, OUTCAR={outcar:g}, OSZICAR={oszicar:g}"
        )
    return {
        "vasprun_energy_eV": vasprun_energy, "outcar_energy_eV": outcar, "oszicar_energy_eV": oszicar,
        "outcar_electronic_converged": outcar_convergence["electronic_converged"],
        "outcar_ionic_converged": outcar_convergence["ionic_converged"],
        "oszicar_final_ionic_step_record": oszicar_convergence["final_ionic_step_record"],
        "oszicar_ionic_step_records": oszicar_convergence["ionic_step_records"],
    }


def collect(root: Path, owner_id: str) -> dict[str, Any]:
    require_owner(root, owner_id)
    manifest, _, state = load_core(root)
    dft_manifest = load_json(root / "dft" / "dft_manifest.json")
    if sha256_file(root / "dft" / "dft_manifest.json") != load_json(root / "dft" / "state" / "dft_state.json")["manifest_sha256"]:
        raise DFTControlError("DFT manifest hash drift")
    if sha256_file(root / dft_manifest["profile_path"]) != dft_manifest["profile_sha256"]:
        raise DFTControlError("VASP profile hash drift; previous preparation and collection evidence are invalid")
    profile = load_json(root / dft_manifest["profile_path"])
    validate_profile(profile)
    energies: dict[str, float] = {}
    completed = failed = active = 0
    active_jobs: list[dict[str, Any]] = []
    for item in dft_manifest["calculations"]:
        checkpoint_path = root / item["checkpoint_path"]
        checkpoint = load_json(checkpoint_path)
        output = root / checkpoint["output_directory"] / "vasprun.xml"
        output_root = root / checkpoint["output_directory"]
        job_path = root / checkpoint["job_status_path"]
        job = load_json(job_path) if job_path.is_file() else None
        job_active = bool(job and job.get("state") in {"queued", "running"})
        if output.is_file():
            try:
                input_manifest = load_json(root / checkpoint["input_manifest_path"])
                titel_path = output_root / "POTCAR.titel"
                digest_path = output_root / "POTCAR.sha256"
                if not titel_path.is_file() or not digest_path.is_file():
                    raise DFTControlError("POTCAR provenance files are missing")
                titel_lines = [line.strip() for line in titel_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                expected_labels = input_manifest["potcar_build"]["element_labels"]
                def titel_has_exact_label(label: str, line: str) -> bool:
                    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(label)}(?![A-Za-z0-9_])", line) is not None
                if len(titel_lines) != len(expected_labels) or any(
                    not titel_has_exact_label(label, line) for label, line in zip(expected_labels, titel_lines)
                ):
                    raise DFTControlError("POTCAR TITEL order does not match frozen element labels")
                potcar_sha = digest_path.read_text(encoding="utf-8").strip().split()[0]
                if not re.fullmatch(r"[0-9a-fA-F]{64}", potcar_sha):
                    raise DFTControlError("POTCAR SHA-256 provenance is invalid")
                if input_manifest["potcar_build"]["backend"] == "vaspkit":
                    version_path = output_root / "VASPKIT.version"
                    expected_version = str(input_manifest["potcar_build"]["validated_vaspkit_version"])
                    if not version_path.is_file() or expected_version not in version_path.read_text(encoding="utf-8", errors="replace"):
                        raise DFTControlError("remote VASPKIT version evidence does not match the frozen profile")
                from dft_profile import effective_incar
                component_profile = dict(profile, incar=effective_incar(profile, item['system_type']))
                result = parse_vasprun(output, component_profile)
                result["output_energy_crosscheck"] = validate_energy_crosschecks(
                    float(result["energy_eV"]), output_root, component_profile
                )
                checkpoint.update(result)
                checkpoint["potcar_sha256"] = potcar_sha.lower()
                checkpoint["potcar_titel_lines"] = titel_lines
                if result["electronic_converged"] and result["ionic_converged"]:
                    checkpoint["status"] = "complete"
                    checkpoint["last_error"] = None
                    energies[item["calculation_id"]] = float(result["energy_eV"])
                    completed += 1
                    if job_active and job_path.is_file():
                        job.update({"state": "completed", "last_checked": iso_time(), "scheduler_evidence": {"reconciled_from": "validated_vasprun.xml"}})
                        atomic_write_json(job_path, job)
                else:
                    if job_active:
                        checkpoint["status"] = job["state"]
                        active += 1
                        active_jobs.append({"calculation_id": item["calculation_id"], "job_id": job.get("job_id"), "pid": job.get("pid"), "state": job["state"]})
                    else:
                        checkpoint["status"] = "failed"
                        failure = "electronic_nonconvergence" if not result["electronic_converged"] else "ionic_nonconvergence"
                        checkpoint.update({"failure_class": failure, "last_error": "electronic or ionic convergence criterion not satisfied"})
                        failed += 1
            except DFTControlError as exc:
                if job_active:
                    checkpoint["last_error"] = str(exc)
                    checkpoint["status"] = job["state"]
                    active += 1
                    active_jobs.append({"calculation_id": item["calculation_id"], "job_id": job.get("job_id"), "pid": job.get("pid"), "state": job["state"]})
                else:
                    checkpoint["status"] = "failed"
                    record_failure(root, checkpoint, output_root, job, str(exc))
                    failed += 1
        elif job_active:
            active += 1
            active_jobs.append({"calculation_id": item["calculation_id"], "job_id": job.get("job_id"), "pid": job.get("pid"), "state": job["state"]})
            checkpoint["status"] = job["state"]
        elif job and job.get("state") in {"finished", "complete", "completed", "failed", "cancelled", "preempted", "out_of_memory", "timeout", "external_timeout", "scheduler_terminal", "output_diagnostic_stop"}:
            checkpoint["status"] = "failed"
            if job.get("state") in {"timeout", "external_timeout"}:
                checkpoint["status"] = "blocked"
                checkpoint["failure_class"] = "scheduler_external_timeout"
                checkpoint["termination_source"] = "scheduler"
                checkpoint["termination_reason"] = "scheduler_external_timeout"
            record_failure(root, checkpoint, output_root, job, f"terminal DPDispatcher job has no valid vasprun.xml (state={job.get('state')})")
            failed += 1
        elif checkpoint["status"] == "complete":
            checkpoint["status"] = "failed"
            record_failure(root, checkpoint, output_root, job, "checkpoint claimed completion but valid output is missing")
            failed += 1
        elif checkpoint["status"] == "failed":
            failed += 1
        elif checkpoint["status"] == "blocked":
            failed += 1
        checkpoint["updated_at"] = iso_time()
        atomic_write_json(checkpoint_path, checkpoint)
    reference_rows: list[dict[str, Any]] = []
    for mapping in dft_manifest["frame_mapping"]:
        ids = [mapping["slab_adsorbate_calculation_id"], mapping["clean_slab_calculation_id"], mapping["isolated_adsorbate_calculation_id"]]
        if all(calculation_id in energies for calculation_id in ids):
            slab_ads, clean, isolated = (energies[item] for item in ids)
            reference_rows.append({
                "structure_index": mapping["structure_index"],
                "slab_composition": mapping["slab_composition"],
                "dft_adsorption_energy_eV": slab_ads - clean - isolated,
                "E_slab_plus_adsorbate_DFT_eV": slab_ads,
                "E_slab_DFT_eV": clean,
                "E_adsorbate_DFT_eV": isolated,
                "slab_adsorbate_calculation_id": ids[0],
                "clean_slab_calculation_id": ids[1],
                "isolated_adsorbate_calculation_id": ids[2],
                "status": "success",
            })
    fields = ["structure_index", "slab_composition", "dft_adsorption_energy_eV", "E_slab_plus_adsorbate_DFT_eV", "E_slab_DFT_eV", "E_adsorbate_DFT_eV", "slab_adsorbate_calculation_id", "clean_slab_calculation_id", "isolated_adsorbate_calculation_id", "status"]
    atomic_write_csv(root / "summary" / "dft_reference.csv", fields, reference_rows)
    dft_state_path = root / "dft" / "state" / "dft_state.json"
    dft_state = load_json(dft_state_path)
    frame_count = int(dft_manifest["frame_count"])
    approval = root / "dft" / "partial_approval.json"
    failed_records, failure_identity = failure_snapshot(root, dft_manifest)
    approval_valid = False
    if approval.is_file():
        approval_record = load_json(approval)
        approval_valid = (
            approval_record.get("manifest_sha256") == dft_state.get("manifest_sha256")
            and approval_record.get("failure_identity_sha256") == failure_identity
            and approval_record.get("failed_calculations") == failed_records
        )
    if len(reference_rows) == frame_count and completed == len(dft_manifest["calculations"]):
        status, next_action = "complete", "merge_benchmark_results"
    elif active:
        status, next_action = "running", "monitor_active_dft_jobs"
    elif failed and approval_valid:
        status, next_action = "partial_approved", "merge_common_valid_frames"
    elif failed:
        status, next_action = "blocked", "recover_failed_dft_or_request_partial_approval"
    else:
        status, next_action = "prepared", "submit_pending_dft_calculations"
    failure_classes: dict[str, int] = {}
    for item in dft_manifest["calculations"]:
        failure_class = load_json(root / item["checkpoint_path"]).get("failure_class")
        if failure_class:
            failure_classes[failure_class] = failure_classes.get(failure_class, 0) + 1
    next_recovery_action = ""
    if failed:
        next_recovery_action = "retry_same_profile_or_request_user_decision"
    dft_state.update({
        "status": status,
        "completed_calculations": completed,
        "failed_calculations": failed,
        "active_calculations": active,
        "valid_frames": len(reference_rows),
        "coverage_fraction": len(reference_rows) / frame_count,
        "active_jobs": active_jobs,
        "failure_classes": failure_classes,
        "next_recovery_action": next_recovery_action or None,
        "partial_approval_path": "dft/partial_approval.json" if approval_valid else None,
        "next_action": next_action,
        "updated_at": iso_time(),
    })
    atomic_write_json(dft_state_path, dft_state)
    state["dft_reference"].update({
        "status": status, "completed_calculations": completed,
        "failed_calculations": failed, "active_calculations": active,
        "valid_frames": len(reference_rows), "coverage_fraction": len(reference_rows) / frame_count,
        "failure_classes": failure_classes,
        "next_recovery_action": next_recovery_action or None,
        "partial_approval_path": dft_state["partial_approval_path"],
        "next_action": next_action, "last_updated": iso_time(),
    })
    append_event(root, state, owner_id, "dft_reference_reconciled", completed=completed, failed=failed, active=active, valid_frames=len(reference_rows))
    state["last_updated"] = iso_time()
    atomic_write_json(root / "run_state.json", state)
    return dft_state


def approve_partial(root: Path, owner_id: str, approved_by: str, reason: str) -> dict[str, Any]:
    require_owner(root, owner_id)
    _, _, state = load_core(root)
    dft_state_path = root / "dft" / "state" / "dft_state.json"
    dft_state = load_json(dft_state_path)
    if dft_state.get("status") != "blocked" or dft_state.get("active_calculations", 0) != 0:
        raise DFTControlError("partial approval requires a blocked DFT branch with no active calculations")
    if dft_state.get("failed_calculations", 0) < 1 or dft_state.get("valid_frames", 0) < 1:
        raise DFTControlError("partial approval requires visible failures and at least one valid DFT frame")
    if approved_by != "user" or not reason.strip():
        raise DFTControlError("partial approval requires approved_by=user and a nonempty reason")
    dft_manifest = load_json(root / "dft" / "dft_manifest.json")
    failed_records, failure_identity = failure_snapshot(root, dft_manifest)
    config = load_json(root / "benchmark_config.json")
    max_attempts = int((config.get("retry_policy") or {}).get("max_attempts") or 0)
    if max_attempts < 1 or any(item["attempt_count"] < max_attempts for item in failed_records):
        raise DFTControlError("partial approval requires bounded retries to reach retry_policy.max_attempts")
    affected_frames = sorted({index for item in failed_records for index in item["frame_indices"]})
    approval = {
        "schema_version": SCHEMA_VERSION, "run_id": dft_state["run_id"],
        "approved_by": approved_by, "approved_at": iso_time(), "reason": reason.strip(),
        "valid_frames": dft_state["valid_frames"], "frame_count": dft_state["frame_count"],
        "coverage_fraction": dft_state["coverage_fraction"],
        "manifest_sha256": dft_state["manifest_sha256"],
        "failure_identity_sha256": failure_identity,
        "failed_calculations": failed_records,
        "affected_frames": affected_frames,
        "retry_policy_max_attempts": max_attempts,
    }
    atomic_write_json(root / "dft" / "partial_approval.json", approval)
    dft_state.update({"status": "partial_approved", "partial_approval_path": "dft/partial_approval.json", "next_action": "merge_common_valid_frames", "updated_at": iso_time()})
    atomic_write_json(dft_state_path, dft_state)
    state["dft_reference"].update({"status": "partial_approved", "partial_approval_path": "dft/partial_approval.json", "next_action": "merge_common_valid_frames", "last_updated": iso_time()})
    append_event(root, state, owner_id, "dft_partial_coverage_approved", valid_frames=dft_state["valid_frames"], frame_count=dft_state["frame_count"])
    state["last_updated"] = iso_time()
    atomic_write_json(root / "run_state.json", state)
    return approval


def submit(root: Path, owner_id: str, dry_run: bool) -> dict[str, Any]:
    descriptor = build_submission(root, owner_id)
    if dry_run or not descriptor["tasks"]:
        return {"dry_run": dry_run, "task_count": len(descriptor["tasks"]), "descriptor_path": "dft/submission_descriptor.json"}
    try:
        from dpdispatcher import Machine, Resources, Submission, Task
    except ImportError as exc:
        raise DFTControlError("DPDispatcher is not installed in the selected execution environment") from exc
    machine = Machine.load_from_dict(descriptor["machine"])
    resources = Resources.load_from_dict(descriptor["resources"])
    tasks = [Task(command=item["command"], task_work_path=item["task_work_path"], forward_files=item["forward_files"], backward_files=item["backward_files"]) for item in descriptor["tasks"]]
    for item in descriptor["tasks"]:
        calc_root = Path(item["local_calculation_root"])
        checkpoint = load_json(calc_root / "checkpoint.json")
        checkpoint.update({"status": "queued", "attempt_count": int(checkpoint["attempt_count"]) + 1, "wrapper_sha256": item.get("wrapper_sha256"), "updated_at": iso_time()})
        atomic_write_json(calc_root / "checkpoint.json", checkpoint)
        atomic_write_json(calc_root / "job_status.json", {
            "schema_version": SCHEMA_VERSION, "run_id": descriptor["run_id"], "calculation_id": item["calculation_id"],
            "backend": "dpdispatcher", "state": "queued", "job_id": None, "pid": os.getpid(),
            "submit_time": iso_time(), "last_checked": iso_time(), "worker_status_path": "dft/submission_descriptor.json", "scheduler_evidence": None,
        })
    stop = threading.Event()
    thread = threading.Thread(target=lambda: _heartbeat_loop(root, owner_id, stop), daemon=True)
    thread.start()
    try:
        submission = Submission(work_base=str((root / "dft" / "calculations").resolve()), machine=machine, resources=resources, task_list=tasks)
        submission.run_submission()
    except Exception as exc:
        for item in descriptor["tasks"]:
            calc_root = Path(item["local_calculation_root"])
            job = load_json(calc_root / "job_status.json")
            job.update({"state": "failed", "last_checked": iso_time(), "scheduler_evidence": {"error": str(exc)}})
            failure_class, _ = classify_failure(calc_root / "outputs", job, str(exc))
            job["failure_class"] = failure_class
            atomic_write_json(calc_root / "job_status.json", job)
            checkpoint = load_json(calc_root / "checkpoint.json")
            checkpoint.update({"status": "failed", "last_error": str(exc), "failure_class": failure_class, "recovery_action": "retry_same_profile_after_scheduler_or_transport_repair", "updated_at": iso_time()})
            record_failure(root, checkpoint, calc_root / "outputs", job, str(exc))
            atomic_write_json(calc_root / "checkpoint.json", checkpoint)
        raise
    else:
        for item in descriptor["tasks"]:
            calc_root = Path(item["local_calculation_root"])
            job = load_json(calc_root / "job_status.json")
            job.update({"state": "queued", "last_checked": iso_time(), "scheduler_evidence": {"submitted": True, "outputs_pending": True}})
            atomic_write_json(calc_root / "job_status.json", job)
    finally:
        stop.set()
        thread.join(timeout=2)
    return {"status": "submitted", "task_count": len(descriptor["tasks"]), "next_action": "reconcile_scheduler_before_collect"}


def _heartbeat_loop(root: Path, owner_id: str, stop: threading.Event) -> None:
    while not stop.wait(60):
        try:
            heartbeat(root, owner_id)
        except Exception:
            return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare and reconcile VASP DFT reference calculations")
    sub = parser.add_subparsers(dest="command", required=True)
    provided_parser = sub.add_parser("validate-provided")
    provided_parser.add_argument("benchmark_root", type=Path)
    provided_parser.add_argument("--owner-id", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("benchmark_root", type=Path)
    prepare_parser.add_argument("--owner-id", required=True)
    prepare_parser.add_argument("--vasp-profile", type=Path, required=True)
    prepare_parser.add_argument("--execution-profile", type=Path, required=True)
    submit_parser = sub.add_parser("submit")
    submit_parser.add_argument("benchmark_root", type=Path)
    submit_parser.add_argument("--owner-id", required=True)
    submit_parser.add_argument("--dry-run", action="store_true")
    preflight_parser = sub.add_parser("preflight")
    preflight_parser.add_argument("benchmark_root", type=Path)
    preflight_parser.add_argument("--owner-id", required=True)
    preflight_parser.add_argument("--runtime-probe", type=Path)
    monitor_parser = sub.add_parser("monitor")
    monitor_parser.add_argument("benchmark_root", type=Path)
    monitor_parser.add_argument("--owner-id", required=True)
    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("benchmark_root", type=Path)
    collect_parser.add_argument("--owner-id", required=True)
    reconcile_parser = sub.add_parser("reconcile")
    reconcile_parser.add_argument("benchmark_root", type=Path)
    reconcile_parser.add_argument("--owner-id", required=True)
    reconcile_parser.add_argument("--scheduler-evidence", type=Path, required=True)
    approve_parser = sub.add_parser("approve-partial")
    approve_parser.add_argument("benchmark_root", type=Path)
    approve_parser.add_argument("--owner-id", required=True)
    approve_parser.add_argument("--approved-by", required=True)
    approve_parser.add_argument("--reason", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        root = args.benchmark_root.resolve()
        if args.command == "validate-provided":
            result = validate_provided(root, args.owner_id)
        elif args.command == "prepare":
            result = prepare(root, args.owner_id, args.vasp_profile, args.execution_profile)
        elif args.command == "submit":
            result = submit(root, args.owner_id, args.dry_run)
        elif args.command == "preflight":
            result = run_preflight(root, args.owner_id, args.runtime_probe)
        elif args.command == "monitor":
            result = monitor_outputs(root, args.owner_id)
        elif args.command == "collect":
            result = collect(root, args.owner_id)
        elif args.command == "reconcile":
            result = reconcile(root, args.owner_id, args.scheduler_evidence)
        else:
            result = approve_partial(root, args.owner_id, args.approved_by, args.reason)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
