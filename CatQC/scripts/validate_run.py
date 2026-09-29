from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import assess_result_row, canonical_json_sha256, load_json, normalize_status, read_csv, sha256_file, validate_frame_artifact
from environment_preparation_control import audit_record, PreparationError
from model_usage_control import validate_smoke_test, validate_usage_plan, UsageError
from run_control import SCHEMA_VERSION, ControlError, request_identity, slugify, task_checkpoint_path, valid_error_indices, valid_result_indices


from catalysis_selection import validate_selection as validate_catalysis, observation_errors

RUN_NAME_RE = re.compile(
    r"^(?P<stamp>\d{8}-\d{6})_(?P<count>[1-9]\d*)models_(?P<adsorbate>[a-z0-9]+(?:-[a-z0-9]+)*)_(?P<task>sp|relax|sp-relax)(?:-(?P<suffix>\d{2}))?$"
)


def require_keys(data: dict[str, Any], keys: list[str], label: str, errors: list[str]) -> None:
    for key in keys:
        if key not in data:
            errors.append(f"{label}: missing key {key}")


def normalized_recorded_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if len(text) >= 2 and text[1] == ":":
        text = text.casefold()
    return text.rstrip("/")


def parse_time(value: Any, label: str, errors: list[str]) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        errors.append(f"{label}: invalid ISO-8601 timestamp {value!r}")
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_model_execution_contract(
    root: Path,
    model: dict[str, Any],
    tasks: list[str],
    errors: list[str],
    script_path_owners: dict[str, str],
    script_hash_owners: dict[str, str],
) -> None:
    model_key = str(model.get("model_key") or "")
    label = f"model {model_key}"
    model_root = root / "models" / model_key
    config_path = model_root / "model_config.json"
    if not config_path.is_file():
        errors.append(f"{label}: missing model_config.json")
        return
    config = load_json(config_path)
    require_keys(config, ["schema_version", "model_key", "model_name", "selection_index", "snapshot_rank", "environment", "checkpoint_storage", "scripts", "calculator_isolation", "official_usage"], label, errors)
    if config.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"{label}: model_config schema_version must be {SCHEMA_VERSION}")
    for field in ("model_key", "model_name", "selection_index", "snapshot_rank"):
        if config.get(field) != model.get(field):
            errors.append(f"{label}: model_config {field} does not match models_manifest")

    if config.get("execution_variant") != model.get("execution_variant"):
        errors.append(f"{label}: model_config execution_variant mismatch")
    official_usage = config.get("official_usage") or {}
    require_keys(official_usage, ["guide_paths", "guide_sha256", "plan_path", "plan_sha256", "plan_file_sha256", "smoke_test_path", "script_generation_verified", "runtime_verified", "official_smoke_test_passed"], f"{label} official usage", errors)
    usage_plan_result: dict[str, Any] | None = None
    plan: dict[str, Any] = {}
    try:
        usage_plan_result = validate_usage_plan(model_root)
        if not usage_plan_result.get("passed"):
            errors.extend(f"{label}: model usage: {error}" for error in usage_plan_result.get("errors") or [])
    except (UsageError, OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"{label}: model usage plan is unreadable: {exc}")
    expected_usage_plan = f"models/{model_key}/resources/model_usage_plan.json"
    expected_smoke_test = f"models/{model_key}/validation/official_usage_smoke_test.json"
    if normalized_recorded_path(official_usage.get("plan_path")) != expected_usage_plan:
        errors.append(f"{label}: official_usage.plan_path must be {expected_usage_plan}")
    if normalized_recorded_path(official_usage.get("smoke_test_path")) != expected_smoke_test:
        errors.append(f"{label}: official_usage.smoke_test_path must be {expected_smoke_test}")
    if official_usage.get("script_generation_verified") is not True or official_usage.get("runtime_verified") is not True or official_usage.get("official_smoke_test_passed") is not True:
        errors.append(f"{label}: official usage gates are not all passed")
    if usage_plan_result and official_usage.get("plan_sha256") != usage_plan_result.get("plan_sha256"):
        errors.append(f"{label}: model usage plan hash does not match model_config")
    if usage_plan_result and official_usage.get("plan_file_sha256") and official_usage.get("plan_file_sha256") != usage_plan_result.get("file_sha256"):
        errors.append(f"{label}: model usage plan file hash does not match model_config")
    recorded_guides = set(official_usage.get("guide_paths") or [])
    if usage_plan_result and recorded_guides != set(usage_plan_result.get("guide_paths") or []):
        errors.append(f"{label}: official usage guide paths do not match frozen usage plan")
    if usage_plan_result:
        plan = load_json(model_root / "resources" / "model_usage_plan.json")
        expected_guide_hashes = plan.get("source_guide_hashes") or {}
        if (official_usage.get("guide_sha256") or {}) != expected_guide_hashes:
            errors.append(f"{label}: official usage guide hashes do not match frozen usage plan")
        preparation_path = model_root / "validation" / "script_preparation.json"
        if not preparation_path.is_file():
            errors.append(f"{label}: missing script_preparation.json")
        else:
            preparation = load_json(preparation_path)
            if preparation.get("execution_variant") != model.get("execution_variant") or preparation.get("catalysis_assessment_sha256") != canonical_json_sha256(model.get("catalysis_assessment")):
                errors.append(f"{label}: script preparation differs from confirmed catalytic configuration")
            if preparation.get("status") != "verified" or preparation.get("usage_plan_sha256") != usage_plan_result.get("plan_sha256"):
                errors.append(f"{label}: script preparation does not match frozen usage plan")
            preparation_scripts = preparation.get("scripts") or {}
            for task, path_field, hash_field in (("single_point", "single_point_path", "single_point_sha256"), ("relaxation", "relaxation_path", "relaxation_sha256")):
                if task in tasks and task in preparation_scripts:
                    item = preparation_scripts[task] or {}
                    if normalized_recorded_path(item.get("path")) != normalized_recorded_path((config.get("scripts") or {}).get(path_field)) or item.get("sha256") != (config.get("scripts") or {}).get(hash_field):
                        errors.append(f"{label}: script preparation evidence does not match {task} script")
        smoke_result = validate_smoke_test(model_root)
        if not smoke_result.get("passed"):
            errors.extend(f"{label}: official usage smoke test: {error}" for error in smoke_result.get("errors") or [])

    environment = config.get("environment") or {}
    preparation_path = model_root / "environment_preparation.json"
    if not preparation_path.is_file():
        errors.append(f"{label}: missing environment_preparation.json; environment creation gate was bypassed")
    else:
        try:
            errors.extend(f"{label}: environment preparation: {error}" for error in audit_record(model_root))
        except (PreparationError, OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{label}: environment preparation record is unreadable: {exc}")
    execution_path = model_root / "environment_execution.json"
    if not execution_path.is_file():
        errors.append(f"{label}: missing environment_execution.json; environment installation was not recorded")
    else:
        try:
            execution = load_json(execution_path)
            if execution.get("schema_version") != SCHEMA_VERSION or execution.get("model_key") != model_key:
                errors.append(f"{label}: environment execution identity/schema is invalid")
            if execution.get("status") != "validated":
                errors.append(f"{label}: environment execution status must be validated")
            if not execution.get("runtime") or not execution["runtime"].get("python_executable"):
                errors.append(f"{label}: environment execution lacks observed runtime evidence")
            gpu_probe = (execution.get("runtime") or {}).get("gpu_probe") or {}
            if gpu_probe.get("passed") is not True or gpu_probe.get("framework") not in {"pytorch", "tensorflow", "jax"}:
                errors.append(f"{label}: GPU runtime probe did not pass")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{label}: environment execution record is unreadable: {exc}")
    require_keys(environment, ["python_executable", "package_versions", "runtime_provenance_path"], f"{label} environment", errors)
    python_executable = normalized_recorded_path(environment.get("python_executable"))
    if not python_executable:
        errors.append(f"{label}: environment python_executable must be nonempty")
    if not isinstance(environment.get("package_versions"), dict) or not environment.get("package_versions"):
        errors.append(f"{label}: package_versions must be a nonempty object")

    scripts = config.get("scripts") or {}
    require_keys(scripts, ["model_specific", "shared_executable_runner", "delegates_to_shared_runner", "owns_model_import", "owns_checkpoint_loading", "owns_calculator_factory", "owns_frame_loop"], f"{label} scripts", errors)
    if scripts.get("model_specific") is not True:
        errors.append(f"{label}: scripts.model_specific must be true")
    if scripts.get("shared_executable_runner") is not False or scripts.get("delegates_to_shared_runner") is not False:
        errors.append(f"{label}: shared or delegated executable runners are forbidden")
    for field in ("owns_model_import", "owns_checkpoint_loading", "owns_calculator_factory", "owns_frame_loop"):
        if scripts.get(field) is not True:
            errors.append(f"{label}: scripts.{field} must be true")

    task_records: dict[str, tuple[str, str]] = {}
    for task, path_field, hash_field in (
        ("single_point", "single_point_path", "single_point_sha256"),
        ("relaxation", "relaxation_path", "relaxation_sha256"),
    ):
        if task not in tasks:
            continue
        relative = normalized_recorded_path(scripts.get(path_field))
        expected_prefix = f"models/{model_key}/scripts/"
        if not relative.startswith(expected_prefix) or relative == expected_prefix:
            errors.append(f"{label}: {task} script must be owned under {expected_prefix}")
            continue
        path = root / relative
        if path.is_symlink() or not path.is_file():
            errors.append(f"{label}: {task} script must be a real regular file")
            continue
        try:
            path.resolve().relative_to((model_root / "scripts").resolve())
        except ValueError:
            errors.append(f"{label}: {task} script resolves outside its model scripts directory")
            continue
        digest = sha256_file(path)
        if scripts.get(hash_field) != digest:
            errors.append(f"{label}: {task} script hash mismatch")
        owner = f"{model_key}/{task}"
        resolved = str(path.resolve())
        if resolved in script_path_owners:
            errors.append(f"{label}: script path is shared with {script_path_owners[resolved]}")
        else:
            script_path_owners[resolved] = owner
        if digest in script_hash_owners:
            errors.append(f"{label}: script content duplicates {script_hash_owners[digest]}")
        else:
            script_hash_owners[digest] = owner
        task_records[task] = (relative, digest)

    provenance_relative = normalized_recorded_path(environment.get("runtime_provenance_path"))
    expected_provenance = f"models/{model_key}/validation/runtime_provenance.json"
    if provenance_relative != expected_provenance:
        errors.append(f"{label}: runtime_provenance_path must be {expected_provenance}")
    provenance_path = root / expected_provenance
    if not provenance_path.is_file():
        errors.append(f"{label}: missing runtime_provenance.json")
    else:
        provenance = load_json(provenance_path)
        require_keys(provenance, ["model_key", "passed", "configured_python_executable", "observed_python_executable", "observed_package_versions", "task_runs", "official_usage"], f"{label} runtime provenance", errors)
        if provenance.get("model_key") != model_key or provenance.get("passed") is not True:
            errors.append(f"{label}: runtime provenance identity/status is invalid")
        if normalized_recorded_path(provenance.get("configured_python_executable")) != python_executable or normalized_recorded_path(provenance.get("observed_python_executable")) != python_executable:
            errors.append(f"{label}: configured and observed Python executables do not match model_config")
        observed_packages = provenance.get("observed_package_versions") or {}
        for package, version in (environment.get("package_versions") or {}).items():
            if str(observed_packages.get(package)) != str(version):
                errors.append(f"{label}: observed package version mismatch for {package}")
        runtime_tasks = provenance.get("task_runs") or {}
        runtime_usage = provenance.get("official_usage") or {}
        if runtime_usage.get("plan_path") != expected_usage_plan or runtime_usage.get("plan_sha256") != official_usage.get("plan_sha256") or runtime_usage.get("plan_file_sha256") != official_usage.get("plan_file_sha256"):
            errors.append(f"{label}: runtime official usage plan evidence does not match model_config")
        if set(runtime_usage.get("guide_paths") or []) != recorded_guides or runtime_usage.get("guide_sha256") != (official_usage.get("guide_sha256") or {}):
            errors.append(f"{label}: runtime official usage guide evidence does not match model_config")
        if runtime_usage.get("official_smoke_test_passed") is not True:
            errors.append(f"{label}: official usage smoke test was not proven at runtime")
        for task, (relative, digest) in task_records.items():
            record = runtime_tasks.get(task) or {}
            errors.extend(f"{label}/{task}: {item}" for item in observation_errors(record, model, root))
            if normalized_recorded_path(record.get("python_executable")) != python_executable:
                errors.append(f"{label}: {task} did not record the configured Python executable")
            if normalized_recorded_path(record.get("script_path")) != relative or record.get("script_sha256") != digest:
                errors.append(f"{label}: {task} runtime script evidence does not match")
            plan_imports = set(plan.get("official_imports") or [])
            observed_imports = set(record.get("imports") or [])
            if not plan_imports.issubset(observed_imports):
                errors.append(f"{label}: {task} runtime imports do not cover official imports")
            checkpoint = record.get("checkpoint_path")
            checkpoint_path = root / str(checkpoint or "")
            if not checkpoint or not checkpoint_path.is_file() or record.get("checkpoint_sha256") != sha256_file(checkpoint_path):
                errors.append(f"{label}: {task} checkpoint runtime evidence is missing or hash-mismatched")
            checkpoint_plan = plan.get("checkpoint_loading") or {}
            if checkpoint_plan.get("path") and normalized_recorded_path(checkpoint) != normalized_recorded_path(checkpoint_plan.get("path")):
                errors.append(f"{label}: {task} checkpoint path does not match usage plan")
            if checkpoint_plan.get("variant") and record.get("checkpoint_variant") != checkpoint_plan.get("variant"):
                errors.append(f"{label}: {task} checkpoint variant does not match usage plan")
            expected_calculator = (plan.get("calculator_factory") or {}).get("class")
            if expected_calculator and record.get("calculator_class") != expected_calculator:
                errors.append(f"{label}: {task} calculator class does not match usage plan")
            expected_device = (plan.get("device_policy") or {}).get("device")
            if expected_device and record.get("device") != expected_device:
                errors.append(f"{label}: {task} device does not match usage plan")
            expected_dtype = (plan.get("dtype_policy") or {}).get("dtype")
            if expected_dtype and record.get("dtype") != expected_dtype:
                errors.append(f"{label}: {task} dtype does not match usage plan")
            units = plan.get("energy_units") or {}
            if record.get("energy_unit") != units.get("energy") or record.get("force_unit") != units.get("force"):
                errors.append(f"{label}: {task} energy/force units do not match usage plan")

    isolation = config.get("calculator_isolation") or {}
    expected_isolation = f"models/{model_key}/validation/script_calculator_isolation.json"
    if normalized_recorded_path(isolation.get("validation_path")) != expected_isolation:
        errors.append(f"{label}: calculator isolation validation_path must be {expected_isolation}")
    isolation_path = root / expected_isolation
    if not isolation_path.is_file():
        errors.append(f"{label}: missing script_calculator_isolation.json")
    else:
        evidence = load_json(isolation_path)
        required = ["model_key", "passed", "script_records", "fresh_calculator_objects", "reuse_mutable_state", "separate_relax_components", "distinct_object_identities", "calculator_factory_call_count", "order_invariance_passed"]
        require_keys(evidence, required, f"{label} isolation evidence", errors)
        if evidence.get("model_key") != model_key or evidence.get("passed") is not True:
            errors.append(f"{label}: calculator isolation evidence identity/status is invalid")
        if evidence.get("fresh_calculator_objects") is not True or evidence.get("reuse_mutable_state") is not False or evidence.get("separate_relax_components") is not True or evidence.get("distinct_object_identities") is not True or evidence.get("order_invariance_passed") is not True:
            errors.append(f"{label}: calculator isolation evidence did not prove isolation")
        if not isinstance(evidence.get("calculator_factory_call_count"), int) or evidence.get("calculator_factory_call_count") < 2:
            errors.append(f"{label}: calculator factory call count must be at least 2")
        for task, (relative, digest) in task_records.items():
            record = (evidence.get("script_records") or {}).get(task) or {}
            if normalized_recorded_path(record.get("path")) != relative or record.get("sha256") != digest:
                errors.append(f"{label}: {task} isolation script evidence does not match")


def validate_event_log(root: Path, state: dict[str, Any], errors: list[str]) -> None:
    path = root / "events.jsonl"
    if not path.is_file():
        errors.append("missing events.jsonl")
        return
    expected = 1
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.endswith("\n"):
                    errors.append(f"events.jsonl line {line_number} is truncated")
                if not line.strip():
                    errors.append(f"events.jsonl line {line_number} is blank")
                    continue
                record = json.loads(line)
                if record.get("schema_version") != SCHEMA_VERSION:
                    errors.append(f"events.jsonl line {line_number} schema_version must be {SCHEMA_VERSION}")
                if record.get("sequence") != expected:
                    errors.append(f"events.jsonl sequence gap at line {line_number}")
                expected += 1
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"events.jsonl is unreadable: {exc}")
        return
    if state.get("last_event_sequence") != expected - 1:
        errors.append("run_state last_event_sequence does not match events.jsonl")


def validate_dft_branch(root: Path, manifest: dict[str, Any], state: dict[str, Any], config: dict[str, Any] | None, errors: list[str]) -> None:
    request = manifest.get("request") or {}
    mode = request.get("dft_reference_mode", "provided")
    summary = state.get("dft_reference")
    if "dft_reference_mode" in request and not isinstance(summary, dict):
        errors.append("new v4 run with dft_reference_mode requires run_state.dft_reference")
        return
    if summary and summary.get("mode") != mode:
        errors.append("run_state DFT reference mode does not match run manifest")
    if config:
        config_request = config.get("benchmark_request") or {}
        configured_mode = config_request.get("dft_reference_mode", "provided")
        if configured_mode != mode:
            errors.append("benchmark_config DFT reference mode does not match run manifest")
    if mode == "provided":
        if config:
            reference = config.get("dft_reference_path")
            reference_path = Path(str(reference)) if reference else None
            if reference_path is not None and not reference_path.is_absolute():
                reference_path = root / reference_path
            if reference_path is None or not reference_path.is_file():
                errors.append("provided DFT mode requires a readable dft_reference_path")
        if summary and summary.get("status") == "provided_ready":
            evidence_path = root / str(summary.get("manifest_path") or "")
            copied_path = root / "summary" / "dft_reference.csv"
            if not evidence_path.is_file() or not copied_path.is_file():
                errors.append("provided_ready DFT state requires frozen table and provenance manifest")
            else:
                evidence = load_json(evidence_path)
                if evidence.get("schema_version") != SCHEMA_VERSION or evidence.get("run_id") != manifest.get("run_id"):
                    errors.append("provided DFT provenance identity mismatch")
                if evidence.get("copied_sha256") != sha256_file(copied_path):
                    errors.append("provided DFT copied-table hash drift")
                rows = read_csv(copied_path)
                frame_count = int((config or {}).get("frame_count") or 0)
                try:
                    indices = [int(row["structure_index"]) for row in rows]
                except (KeyError, TypeError, ValueError):
                    indices = []
                    errors.append("provided DFT table contains an invalid structure_index")
                if frame_count and indices != list(range(frame_count)):
                    errors.append("provided DFT table does not cover every frame in order")
                if summary.get("valid_frames") != len(rows) or summary.get("coverage_fraction") != 1.0:
                    errors.append("provided DFT central summary does not match frozen table")
        if manifest.get("status") == "complete" and summary and summary.get("status") != "provided_ready":
            errors.append("complete provided-DFT run requires provided_ready DFT state")
        return
    if not config:
        return
    dft_root = root / "dft"
    required = [dft_root / "vasp_profile.json", dft_root / "execution_profile.json", dft_root / "dft_manifest.json", dft_root / "state" / "dft_state.json", dft_root / "execution_preflight.json"]
    missing = [relative(path, root) for path in required if not path.is_file()]
    if missing:
        if summary and summary.get("status") == "not_prepared" and len(missing) == len(required):
            return
        errors.append(f"generated VASP DFT branch is missing artifacts: {missing}")
        return
    profile = load_json(required[0])
    execution = load_json(required[1])
    dft_manifest = load_json(required[2])
    dft_state = load_json(required[3])
    preflight = load_json(required[4])
    if any(item.get("schema_version") != SCHEMA_VERSION for item in (profile, execution, dft_manifest, dft_state)):
        errors.append("all generated DFT artifacts must use schema_version 4")
    from dft_reference_control import validate_profile, DFTControlError
    try:
        validate_profile(profile)
    except (ValueError, KeyError, TypeError, DFTControlError) as exc:
        errors.append(f"invalid confirmed DFT profile: {exc}")
    if profile.get("confirmed") is not True or profile.get("confirmed_by") != "user":
        errors.append("VASP profile is not explicitly user-confirmed")
    if execution.get("credentials_policy") != "external_only":
        errors.append("DFT execution profile must use external_only credentials")
    secret_keys = [key for key in json.dumps(execution, ensure_ascii=False).split('"') if re.search(r"password|passwd|token|secret|private.?key", key, re.I)]
    if secret_keys:
        errors.append("DFT execution profile appears to contain credential fields")
    if dft_manifest.get("profile_sha256") != sha256_file(required[0]) or dft_manifest.get("execution_profile_sha256") != sha256_file(required[1]):
        errors.append("DFT profile or execution profile hash drift")
    if dft_state.get("manifest_sha256") != sha256_file(required[2]):
        errors.append("DFT manifest hash drift")
    if preflight.get("status") != "passed" or preflight.get("execution_profile_sha256") != sha256_file(required[1]):
        errors.append("DFT execution preflight is missing, failed, or stale")
    if dft_state.get("preflight_status") != "passed":
        errors.append("DFT state does not record a passed execution preflight")
    calculations = dft_manifest.get("calculations") or []
    ids = [item.get("calculation_id") for item in calculations]
    if not calculations or len(ids) != len(set(ids)) or any(not item for item in ids):
        errors.append("DFT calculation IDs must be nonempty and unique")
    frame_count = int(dft_manifest.get("frame_count") or 0)
    mappings = dft_manifest.get("frame_mapping") or []
    try:
        mapping_indices = [int(item["structure_index"]) for item in mappings]
    except (KeyError, TypeError, ValueError):
        mapping_indices = []
    if mapping_indices != list(range(frame_count)):
        errors.append("DFT frame mapping must cover every structure index exactly once in order")
    id_set = set(ids)
    for mapping in mappings:
        if not str(mapping.get("slab_composition") or "").strip():
            errors.append(f"DFT frame mapping lacks slab composition at index {mapping.get('structure_index')}")
        for field in ("slab_adsorbate_calculation_id", "clean_slab_calculation_id", "isolated_adsorbate_calculation_id"):
            if mapping.get(field) not in id_set:
                errors.append(f"DFT frame mapping references unknown calculation: {mapping.get(field)}")
    completed = failed = active = 0
    active_jobs: list[tuple[Any, Any, Any]] = []
    for item in calculations:
        checkpoint_path = root / str(item.get("checkpoint_path") or "")
        if not checkpoint_path.is_file():
            errors.append(f"missing DFT checkpoint: {item.get('checkpoint_path')}")
            continue
        checkpoint = load_json(checkpoint_path)
        if checkpoint.get("schema_version") != SCHEMA_VERSION or checkpoint.get("run_id") != manifest.get("run_id") or checkpoint.get("calculation_id") != item.get("calculation_id"):
            errors.append(f"DFT checkpoint identity mismatch: {item.get('calculation_id')}")
        if checkpoint.get("structure_sha256") != item.get("structure_sha256") or checkpoint.get("profile_sha256") != dft_manifest.get("profile_sha256"):
            errors.append(f"DFT checkpoint structure/profile hash mismatch: {item.get('calculation_id')}")
        input_manifest_path = root / str(checkpoint.get("input_manifest_path") or "")
        if not input_manifest_path.is_file():
            errors.append(f"missing DFT input manifest: {item.get('calculation_id')}")
        else:
            input_manifest = load_json(input_manifest_path)
            if input_manifest.get("calculation_id") != item.get("calculation_id") or input_manifest.get("structure_sha256") != item.get("structure_sha256"):
                errors.append(f"DFT input manifest identity mismatch: {item.get('calculation_id')}")
            for name, record in (input_manifest.get("input_files") or {}).items():
                input_path = input_manifest_path.parent / "inputs" / name
                if not input_path.is_file() or input_path.stat().st_size != record.get("size_bytes") or sha256_file(input_path) != record.get("sha256"):
                    errors.append(f"DFT frozen input hash mismatch: {item.get('calculation_id')}/{name}")
        status = checkpoint.get("status")
        if status == "complete":
            energy = checkpoint.get("energy_eV")
            if not isinstance(energy, (int, float)) or not math.isfinite(float(energy)) or checkpoint.get("electronic_converged") is not True or checkpoint.get("ionic_converged") is not True:
                errors.append(f"complete DFT calculation lacks valid converged energy: {item.get('calculation_id')}")
            if not re.fullmatch(r"[0-9a-f]{64}", str(checkpoint.get("potcar_sha256") or "")) or not checkpoint.get("potcar_titel_lines"):
                errors.append(f"complete DFT calculation lacks POTCAR provenance: {item.get('calculation_id')}")
            completed += 1
        elif status == "failed":
            failed += 1
        elif status in {"queued", "running"}:
            active += 1
            job_path = root / str(checkpoint.get("job_status_path") or "")
            if not job_path.is_file():
                errors.append(f"active DFT calculation lacks job status: {item.get('calculation_id')}")
            else:
                job = load_json(job_path)
                if job.get("job_id") is None and job.get("pid") is None:
                    errors.append(f"active DFT job lacks job ID or PID: {item.get('calculation_id')}")
                active_jobs.append((item.get("calculation_id"), job.get("job_id"), job.get("pid")))
    expected_counts = {
        "expected_calculations": len(calculations), "completed_calculations": completed,
        "failed_calculations": failed, "active_calculations": active,
    }
    for field, value in expected_counts.items():
        if dft_state.get(field) != value:
            errors.append(f"DFT state {field} does not match calculation checkpoints")
        if summary and summary.get(field) != value:
            errors.append(f"central DFT summary {field} does not match calculation checkpoints")
    recorded_jobs = sorted((item.get("calculation_id"), item.get("job_id"), item.get("pid")) for item in dft_state.get("active_jobs") or [])
    if recorded_jobs != sorted(active_jobs):
        errors.append("DFT active job summary does not match calculation job statuses")
    reference_path = root / "summary" / "dft_reference.csv"
    valid_frames = 0
    if reference_path.is_file():
        rows = read_csv(reference_path)
        indices = [int(row["structure_index"]) for row in rows]
        if indices != sorted(set(indices)):
            errors.append("generated dft_reference.csv contains duplicate or unsorted frame indices")
        valid_frames = len(rows)
    if dft_state.get("valid_frames") != valid_frames or (summary and summary.get("valid_frames") != valid_frames):
        errors.append("DFT valid-frame summary does not match dft_reference.csv")
    expected_fraction = valid_frames / frame_count if frame_count else 0.0
    if dft_state.get("coverage_fraction") != expected_fraction or (summary and summary.get("coverage_fraction") != expected_fraction):
        errors.append("DFT coverage fraction is inconsistent")
    if valid_frames < frame_count and dft_state.get("status") == "partial_approved":
        approval_path = root / str(dft_state.get("partial_approval_path") or "")
        approval = load_json(approval_path) if approval_path.is_file() else {}
        failure_records = []
        for item in calculations:
            checkpoint = load_json(root / item["checkpoint_path"])
            if checkpoint.get("status") == "failed":
                failure_records.append({
                    "calculation_id": item["calculation_id"],
                    "frame_indices": sorted(item.get("frame_indices") or []),
                    "attempt_count": int(checkpoint.get("attempt_count") or 0),
                    "last_error": checkpoint.get("last_error"),
                })
        failure_records.sort(key=lambda item: item["calculation_id"])
        if approval.get("approved_by") != "user" or approval.get("manifest_sha256") != dft_state.get("manifest_sha256") or approval.get("failure_identity_sha256") != canonical_json_sha256(failure_records) or approval.get("failed_calculations") != failure_records:
            errors.append("partial DFT coverage lacks explicit user approval")
    if manifest.get("status") == "complete" and dft_state.get("status") not in {"complete", "partial_approved"}:
        errors.append("complete run requires complete or explicitly approved partial DFT coverage")


def relative(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def validate(root: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    manifest_path = root / "run_manifest.json"
    if not manifest_path.is_file():
        return {"passed": False, "errors": ["missing run_manifest.json"], "warnings": warnings}
    manifest = load_json(manifest_path)
    version = manifest.get("schema_version")
    if version != SCHEMA_VERSION:
        if version in {2, 3}:
            errors.append(f"incompatible run schema_version {version}; v2/v3 runs cannot be resumed and must be restarted")
        else:
            errors.append(f"manifest schema_version must be {SCHEMA_VERSION}")
        return {"passed": False, "errors": errors, "warnings": warnings}
    if manifest.get("skill_name") != "catqc":
        return {"passed": False, "errors": ["not a CatQC run; use the original skill release for historical runs"], "warnings": warnings}
    require_keys(manifest, ["run_id", "status", "request", "request_identity_sha256", "models_manifest_sha256", "workspace_base", "run_base", "benchmark_root", "scientific_definition_hash"], "manifest", errors)
    request = manifest.get("request") or {}
    require_keys(request, ["model_selection_mode", "dataset_name", "dataset_identity", "adsorbate", "model_count", "requested_tasks", "task_slug"], "manifest request", errors)
    workspace_base = Path(str(manifest.get("workspace_base", ""))).resolve()
    expected_run_base = (workspace_base / "CatQC" / "benchmark_runs").resolve()
    if Path(str(manifest.get("run_base", ""))).resolve() != expected_run_base:
        errors.append("manifest run_base must equal <workspace_base>/CatQC/benchmark_runs")
    if root.parent.resolve() != expected_run_base or Path(str(manifest.get("benchmark_root", ""))).resolve() != root.resolve():
        errors.append("benchmark_root must be the recorded direct child of run_base")
    match = RUN_NAME_RE.fullmatch(root.name)
    if not match:
        errors.append("benchmark directory name does not match YYYYMMDD-HHMMSS_<N>models_<adsorbate>_<task>")
    else:
        if int(match.group("count")) != request.get("model_count"):
            errors.append("directory model count does not match manifest")
        try:
            expected_adsorbate_slug = slugify(str(request.get("adsorbate") or ""))
        except Exception:
            expected_adsorbate_slug = ""
        if match.group("adsorbate") != expected_adsorbate_slug or match.group("task") != request.get("task_slug"):
            errors.append("directory adsorbate/task slug does not match manifest")

    models_path = root / "models_manifest.json"
    state_path = root / "run_state.json"
    if not models_path.is_file() or not state_path.is_file():
        errors.extend([name for path, name in ((models_path, "missing models_manifest.json"), (state_path, "missing run_state.json")) if not path.is_file()])
        return {"passed": False, "errors": errors, "warnings": warnings}
    models_manifest = load_json(models_path)
    state = load_json(state_path)
    if models_manifest.get("schema_version") != SCHEMA_VERSION or state.get("schema_version") != SCHEMA_VERSION:
        errors.append("models_manifest.json and run_state.json must use schema_version 4")
        return {"passed": False, "errors": errors, "warnings": warnings}
    if manifest.get("models_manifest_sha256") != canonical_json_sha256(models_manifest):
        errors.append("models_manifest.json hash does not match run_manifest.json")
    models = models_manifest.get("models") or []
    if models_manifest.get("model_count") != len(models) or request.get("model_count") != len(models):
        errors.append("model counts disagree across manifest and models_manifest")
    if [model.get("selection_index") for model in models] != list(range(1, len(models) + 1)):
        errors.append("models_manifest selection indices must be contiguous from 1 through N")
    keys = [model.get("model_key") for model in models]
    if len(keys) != len(set(keys)) or any(not key for key in keys):
        errors.append("models_manifest model_key values must be nonempty and unique")
    for model in models:
        errors.extend(f"model {model.get('model_key')}: {item}" for item in validate_catalysis(model, models_manifest.get("model_selection_mode")))
        identity = dict(model)
        recorded = identity.pop("identity_sha256", None)
        if recorded != canonical_json_sha256(identity):
            errors.append(f"model {model.get('model_key')}: identity_sha256 mismatch")
        if models_manifest.get("model_selection_mode") == "user_specified":
            if model.get("snapshot_rank") is not None or model.get("confirmed") is not True or model.get("confirmed_by") != "user" or not model.get("identity_url"):
                errors.append(f"model {model.get('model_key')}: named-model identity is not user-confirmed")
    for evidence in models_manifest.get("selection_evidence") or []:
        path = root / str(evidence.get("path") or "")
        if not path.is_file() or path.stat().st_size != evidence.get("size_bytes") or sha256_file(path) != evidence.get("sha256"):
            errors.append(f"selection evidence verification failed: {evidence.get('path')}")
    if "dft_reference_mode" in request:
        expected_request_identity = request_identity(request.get("dataset_identity", ""), request.get("adsorbate", ""), request.get("requested_tasks") or [], models_manifest, request.get("dft_reference_mode", "provided"))
    else:
        expected_request_identity = request_identity(request.get("dataset_identity", ""), request.get("adsorbate", ""), request.get("requested_tasks") or [], models_manifest, include_dft_mode=False)
    if manifest.get("request_identity_sha256") != expected_request_identity or state.get("request_identity_sha256") != expected_request_identity:
        errors.append("request_identity_sha256 mismatch")
    if set(state.get("models") or {}) != set(keys):
        errors.append("run_state model keys do not match models_manifest")

    validate_event_log(root, state, errors)
    lease_path = root / "control" / "lease.json"
    if not lease_path.is_file():
        errors.append("missing control/lease.json")
    else:
        lease = load_json(lease_path)
        if lease.get("schema_version") != SCHEMA_VERSION or lease.get("run_id") != manifest.get("run_id"):
            errors.append("lease identity/schema is invalid")
        expires = parse_time(lease.get("expires_at"), "lease expires_at", errors)
        if state.get("submission_allowed"):
            if lease.get("status") != "active" or (expires and expires <= datetime.now(timezone.utc)):
                errors.append("submission_allowed requires a nonexpired active lease")
            if state.get("reconciliation_required"):
                errors.append("submission_allowed cannot be true while reconciliation is required")

    config_path = root / "benchmark_config.json"
    config: dict[str, Any] | None = None
    if config_path.is_file():
        config = load_json(config_path)
        from rank_models import full_indices
        try:
            full_indices(config)
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"invalid frozen structure index list: {exc}")
        if config.get("schema_version") != SCHEMA_VERSION:
            errors.append("benchmark_config schema_version must be 4")
        config_request = config.get("benchmark_request") or {}
        for field in ("model_selection_mode", "model_count", "dataset_name", "adsorbate", "requested_tasks"):
            if config_request.get(field) != request.get(field):
                errors.append(f"benchmark_config request {field} does not match run manifest")
        expected_hash = canonical_json_sha256(config)
        if manifest.get("scientific_definition_hash") and manifest.get("scientific_definition_hash") != expected_hash:
            errors.append("scientific_definition_hash does not match benchmark_config.json")
    elif manifest.get("status") not in {"initialized", "blocked"}:
        errors.append("active/complete run requires benchmark_config.json")

    validate_dft_branch(root, manifest, state, config, errors)

    frame_count = state.get("frame_count")
    tasks = request.get("requested_tasks") or []
    script_path_owners: dict[str, str] = {}
    script_hash_owners: dict[str, str] = {}
    expected_active_jobs: list[tuple[str, str, Any, Any]] = []
    for model in models:
        model_key = model["model_key"]
        model_state = (state.get("models") or {}).get(model_key) or {}
        summaries = model_state.get("tasks") or {}
        for task in ("single_point", "relaxation"):
            checkpoint_path = root / task_checkpoint_path(model_key, task)
            if not checkpoint_path.is_file():
                errors.append(f"missing {checkpoint_path.relative_to(root)}")
                continue
            checkpoint = load_json(checkpoint_path)
            if checkpoint.get("schema_version") != SCHEMA_VERSION or checkpoint.get("run_id") != manifest.get("run_id") or checkpoint.get("model_key") != model_key or checkpoint.get("task") != task:
                errors.append(f"{checkpoint_path.relative_to(root)} identity/schema is invalid")
                continue
            if checkpoint.get("frame_artifacts_required") is not True:
                errors.append(f"{model_key}/{task}: frame_artifacts_required must be true")
            if checkpoint.get("frame_directory_template") != f"models/{model_key}/frames/{task}/<structure_index:08d>":
                errors.append(f"{model_key}/{task}: frame_directory_template is invalid")
            expected_status = "not_requested" if task not in tasks else None
            if expected_status and checkpoint.get("status") != expected_status:
                errors.append(f"{model_key}/{task}: unrequested task must be not_requested")
            if frame_count is not None and task in tasks and checkpoint.get("expected_frame_count") != frame_count:
                errors.append(f"{model_key}/{task}: expected_frame_count does not match run_state")
            completed = checkpoint.get("completed_indices") or []
            failed = checkpoint.get("failed_indices") or []
            active = checkpoint.get("active_indices") or []
            if frame_count is not None and task in tasks:
                for index in sorted(set(completed) | set(failed)):
                    artifact = validate_frame_artifact(
                        root, model_key, task, index,
                        expected_run_id=manifest.get("run_id"),
                        expected_frame_count=frame_count,
                    )
                    if not artifact["valid"]:
                        errors.extend(
                            f"{model_key}/{task}/{index}: {reason}"
                            for reason in artifact["errors"]
                        )
            for name, values in (("completed", completed), ("failed", failed), ("active", active)):
                if values != sorted(set(values)):
                    errors.append(f"{model_key}/{task}: {name}_indices must be sorted and unique")
                if frame_count is not None and any(index < 0 or index >= frame_count for index in values):
                    errors.append(f"{model_key}/{task}: {name}_indices contains an out-of-range frame")
            if set(completed) & set(failed) or set(completed) & set(active):
                errors.append(f"{model_key}/{task}: completed frames overlap failed/active frames")
            try:
                result_completed, result_failed, result_hash = valid_result_indices(root, checkpoint, config)
            except (ControlError, ValueError, OSError) as exc:
                errors.append(f"{model_key}/{task}: {exc}")
                result_completed, result_failed, result_hash = set(), set(), None
            error_failed = valid_error_indices(root, checkpoint)
            if set(completed) != result_completed:
                errors.append(f"{model_key}/{task}: checkpoint completed_indices do not equal validated result rows")
            if set(failed) != (result_failed | error_failed) - result_completed:
                errors.append(f"{model_key}/{task}: failed_indices do not equal persisted failed result/error records")
            if result_hash != checkpoint.get("last_result_sha256") and (root / checkpoint.get("result_path", "")).is_file():
                errors.append(f"{model_key}/{task}: result hash does not match checkpoint")
            if checkpoint.get("status") == "complete":
                if frame_count is None or len(completed) != frame_count or failed or active:
                    errors.append(f"{model_key}/{task}: complete checkpoint has incomplete or conflicting coverage")
            summary = summaries.get(task) or {}
            if summary.get("checkpoint_path") != str(checkpoint_path.relative_to(root)).replace("\\", "/"):
                errors.append(f"{model_key}/{task}: run_state checkpoint path mismatch")
            if summary.get("n_completed") != len(completed) or summary.get("n_failed") != len(failed) or summary.get("n_active") != len(active) or summary.get("status") != checkpoint.get("status"):
                errors.append(f"{model_key}/{task}: run_state summary does not match checkpoint")
            job_path = root / str(checkpoint.get("job_status_path") or "")
            if job_path.is_file():
                job = load_json(job_path)
                if job.get("schema_version") != SCHEMA_VERSION or job.get("model_key") != model_key or job.get("task") != task:
                    errors.append(f"{model_key}/{task}: job status identity/schema mismatch")
                if job.get("state") in {"queued", "running"}:
                    if job.get("job_id") is None and job.get("pid") is None:
                        errors.append(f"{model_key}/{task}: active job requires job_id or pid")
                    if set(job.get("active_indices") or []) != set(active):
                        errors.append(f"{model_key}/{task}: job active_indices do not match checkpoint")
                    expected_active_jobs.append((model_key, task, job.get("job_id"), job.get("pid")))
        result_exists = any((root / task_checkpoint_path(model_key, task)).is_file() and (root / task_result_path(model_key, task)).is_file() for task in tasks)
        if result_exists or model_state.get("environment_status") == "ready" or manifest.get("status") == "complete":
            validate_model_execution_contract(root, model, tasks, errors, script_path_owners, script_hash_owners)

    recorded_active = sorted((item.get("model_key"), item.get("task"), item.get("job_id"), item.get("pid")) for item in state.get("active_jobs") or [])
    if recorded_active != sorted(expected_active_jobs):
        errors.append("run_state active_jobs does not match active job status artifacts")
    requested_summaries = [
        task_summary
        for model_state in (state.get("models") or {}).values()
        for task_summary in (model_state.get("tasks") or {}).values()
        if task_summary.get("status") != "not_requested"
    ]
    expected_total = (int(frame_count) * len(requested_summaries)) if frame_count is not None else None
    expected_progress = {
        "expected_frames": expected_total,
        "completed_frames": sum(int(item.get("n_completed") or 0) for item in requested_summaries),
        "failed_frames": sum(int(item.get("n_failed") or 0) for item in requested_summaries),
        "active_frames": sum(int(item.get("n_active") or 0) for item in requested_summaries),
    }
    progress = state.get("progress") or {}
    for field, value in expected_progress.items():
        if progress.get(field) != value:
            errors.append(f"run_state progress {field} does not match task summaries")
    expected_fraction = (expected_progress["completed_frames"] / expected_total) if expected_total else 0.0
    if progress.get("coverage_fraction") != expected_fraction:
        errors.append("run_state progress coverage_fraction is inconsistent")
    if manifest.get("status") == "complete" and state.get("status") != "complete":
        errors.append("complete manifest requires complete run_state")
    return {"passed": not errors, "errors": errors, "warnings": warnings}


def task_result_path(model_key: str, task: str) -> str:
    label = "SP" if task == "single_point" else "Relax"
    return f"models/{model_key}/results/adsorption_energy_{label}.csv"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("benchmark_root", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = validate(args.benchmark_root.resolve())
    except Exception as exc:
        result = {"passed": False, "errors": [f"validator exception: {exc}"], "warnings": []}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for warning in result["warnings"]:
            print(f"WARNING: {warning}")
        for error in result["errors"]:
            print(f"ERROR: {error}")
        print("Run validation passed" if result["passed"] else "Run validation failed")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
