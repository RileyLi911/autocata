from __future__ import annotations

import argparse
import json
import urllib.parse
from pathlib import Path
from typing import Any

from common import atomic_write_json, atomic_write_text, canonical_json_sha256, load_json, sha256_file
from environment_preparation_control import audit_record, load_record, model_context, host_matches


from catalysis_selection import binding_errors, frozen_model, observation_errors

SCHEMA_VERSION = 4
USAGE_FIELDS = (
    "source_guide_paths", "official_imports", "checkpoint_loading",
    "calculator_factory", "device_policy", "dtype_policy", "energy_units",
    "single_point_protocol", "relaxation_protocol", "official_smoke_tests",
    "derived_adaptations", "execution_variant", "catalysis_assessment_sha256",
)


class UsageError(RuntimeError):
    pass


def plan_path(model_root: Path) -> Path:
    return model_root / "resources" / "model_usage_plan.json"


def plan_hash_path(model_root: Path) -> Path:
    return model_root / "resources" / "model_usage_plan.sha256"


def _guide_records(model_root: Path) -> list[dict[str, Any]]:
    return [item for item in (load_record(model_root).get("official_guides") or []) if item.get("scope") in {"usage", "both"}]


def _verify_guides(model_root: Path, required_paths: set[str] | None = None) -> list[str]:
    run_root, _ = model_context(model_root)
    errors: list[str] = []
    guides = _guide_records(model_root)
    if not guides:
        return ["no official model-usage guide is recorded"]
    seen: set[str] = set()
    for guide in guides:
        path_text = str(guide.get("artifact_path") or "")
        seen.add(path_text)
        path = run_root / path_text
        if not path.is_file():
            errors.append(f"missing official model-usage guide: {path_text}")
        elif sha256_file(path) != guide.get("content_sha256"):
            errors.append(f"official model-usage guide hash mismatch: {path_text}")
        allowed_hosts = [str(host) for host in guide.get("official_hosts") or []]
        for field in ("source_url", "final_url"):
            host = urllib.parse.urlparse(str(guide.get(field) or "")).hostname or ""
            if not allowed_hosts or not host_matches(host, allowed_hosts):
                errors.append(f"{field} host is outside official_hosts: {path_text}")
    if required_paths and not required_paths.issubset(seen):
        errors.append("model usage plan references an unrecorded official guide")
    return errors


def freeze_usage_plan(args: argparse.Namespace) -> dict[str, Any]:
    model_root = args.model_root.resolve()
    run_root, model_key = model_context(model_root)
    source = args.plan_source.resolve()
    if not source.is_file():
        raise UsageError(f"usage plan source does not exist: {source}")
    plan = load_json(source)
    missing = [field for field in USAGE_FIELDS if field not in plan]
    if missing:
        raise UsageError(f"model usage plan is missing fields: {missing}")
    if not isinstance(plan["source_guide_paths"], list) or not plan["source_guide_paths"]:
        raise UsageError("source_guide_paths must be nonempty")
    shape_errors = validate_plan_shape(plan) + binding_errors(model_root, plan)
    if shape_errors:
        raise UsageError("; ".join(shape_errors))
    conflicts = plan.get("conflicts") or plan.get("unresolved_conflicts") or []
    if conflicts:
        raise UsageError("unresolved official-guide conflicts block usage-plan freeze")
    guide_errors = _verify_guides(model_root, set(plan["source_guide_paths"]))
    if guide_errors:
        raise UsageError("; ".join(guide_errors))
    frozen = dict(plan)
    frozen["schema_version"] = SCHEMA_VERSION
    frozen["model_key"] = model_key
    frozen["status"] = "frozen"
    frozen["source_guide_hashes"] = {
        item["artifact_path"]: item["content_sha256"]
        for item in _guide_records(model_root)
        if item.get("artifact_path") in set(plan["source_guide_paths"])
    }
    frozen["plan_sha256"] = canonical_json_sha256({key: value for key, value in frozen.items() if key != "plan_sha256"})
    destination = plan_path(model_root)
    atomic_write_json(destination, frozen)
    file_hash = sha256_file(destination)
    atomic_write_text(plan_hash_path(model_root), file_hash)
    return {
        "model_key": model_key,
        "plan_path": str(destination.relative_to(run_root)).replace("\\", "/"),
        "plan_sha256": frozen["plan_sha256"],
        "file_sha256": file_hash,
        "source_guide_paths": frozen["source_guide_paths"],
    }


def validate_usage_plan(model_root: Path) -> dict[str, Any]:
    model_root = model_root.resolve()
    run_root, model_key = model_context(model_root)
    errors = _verify_guides(model_root)
    path = plan_path(model_root)
    hash_path = plan_hash_path(model_root)
    if not path.is_file():
        errors.append("missing model_usage_plan.json")
        return {"passed": False, "errors": errors}
    if not hash_path.is_file():
        errors.append("missing model_usage_plan.sha256")
    else:
        recorded_file_hash = hash_path.read_text(encoding="utf-8-sig").strip()
        if recorded_file_hash != sha256_file(path):
            errors.append("model_usage_plan.sha256 does not match model_usage_plan.json")
    try:
        plan = load_json(path)
    except Exception as exc:
        errors.append(f"model_usage_plan.json is unreadable: {exc}")
        return {"passed": False, "errors": errors}
    if plan.get("schema_version") != SCHEMA_VERSION or plan.get("model_key") != model_key or plan.get("status") != "frozen":
        errors.append("model_usage_plan identity/status is invalid")
    missing = [field for field in USAGE_FIELDS if field not in plan]
    if missing:
        errors.append(f"model_usage_plan is missing fields: {missing}")
    errors.extend(validate_plan_shape(plan))
    errors.extend(binding_errors(model_root, plan))
    guide_errors = _verify_guides(model_root, set(plan.get("source_guide_paths") or []))
    errors.extend(guide_errors)
    content = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if plan.get("plan_sha256") != canonical_json_sha256(content):
        errors.append("model_usage_plan plan_sha256 mismatch")
    return {
        "passed": not errors,
        "errors": errors,
        "plan_path": str(path.relative_to(run_root)).replace("\\", "/"),
        "plan_sha256": plan.get("plan_sha256"),
        "file_sha256": sha256_file(path) if path.is_file() else None,
        "guide_paths": plan.get("source_guide_paths") or [],
    }


def validate_plan_shape(plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(plan.get("source_guide_paths"), list) or not plan.get("source_guide_paths"):
        errors.append("source_guide_paths must be a nonempty array")
    for field in ("official_imports", "official_smoke_tests", "derived_adaptations"):
        if not isinstance(plan.get(field), list) or not plan.get(field):
            errors.append(f"{field} must be a nonempty array")
    for field in ("checkpoint_loading", "calculator_factory", "device_policy", "dtype_policy", "energy_units", "single_point_protocol", "relaxation_protocol"):
        if not isinstance(plan.get(field), dict) or not plan.get(field):
            errors.append(f"{field} must be a nonempty object")
    checkpoint = plan.get("checkpoint_loading") or {}
    if not checkpoint.get("source") or not checkpoint.get("variant") or not checkpoint.get("verification"):
        errors.append("checkpoint_loading must define source, variant, and verification")
    calculator = plan.get("calculator_factory") or {}
    if not calculator.get("class") and not calculator.get("factory"):
        errors.append("calculator_factory must define class or factory")
    if not (plan.get("device_policy") or {}).get("device"):
        errors.append("device_policy must define device")
    if not (plan.get("dtype_policy") or {}).get("dtype"):
        errors.append("dtype_policy must define dtype")
    if not (plan.get("single_point_protocol") or {}).get("entrypoint"):
        errors.append("single_point_protocol must define entrypoint")
    if not (plan.get("relaxation_protocol") or {}).get("entrypoint"):
        errors.append("relaxation_protocol must define entrypoint")
    units = plan.get("energy_units") or {}
    if not units.get("energy") or not units.get("force"):
        errors.append("energy_units must define energy and force")
    adaptations = plan.get("derived_adaptations") or []
    for index, item in enumerate(adaptations):
        if not isinstance(item, dict) or not item.get("source") or not item.get("change") or not item.get("reason"):
            errors.append(f"derived_adaptations[{index}] requires source, change, and reason")
    return errors


def smoke_test_path(model_root: Path) -> Path:
    return model_root / "validation" / "official_usage_smoke_test.json"


def validate_smoke_test(model_root: Path) -> dict[str, Any]:
    run_root, model_key = model_context(model_root)
    path = smoke_test_path(model_root)
    errors: list[str] = []
    if not path.is_file():
        return {"passed": False, "errors": ["missing official_usage_smoke_test.json"]}
    try:
        evidence = load_json(path)
    except Exception as exc:
        return {"passed": False, "errors": [f"official usage smoke test is unreadable: {exc}"]}
    required = ("schema_version", "model_key", "passed", "command", "python_executable", "imports", "checkpoint_path", "checkpoint_variant", "checkpoint_sha256", "calculator_class", "device", "dtype", "energy_unit", "force_unit", "exit_code", "stdout_path", "stderr_path", "started_at", "finished_at", "guide_paths", "guide_sha256", "plan_sha256", "plan_file_sha256", "adaptations")
    errors.extend(f"smoke test missing {field}" for field in required if field not in evidence)
    if evidence.get("schema_version") != SCHEMA_VERSION or evidence.get("model_key") != model_key or evidence.get("passed") is not True or evidence.get("exit_code") != 0:
        errors.append("official usage smoke test identity/status/exit_code is invalid")
    try:
        errors.extend(observation_errors(evidence, frozen_model(model_root), run_root, all_components=False))
    except (OSError, ValueError, KeyError) as exc:
        errors.append(str(exc))
    usage = validate_usage_plan(model_root)
    if not usage.get("passed"):
        errors.extend(f"usage plan: {item}" for item in usage.get("errors") or [])
    if evidence.get("plan_sha256") != usage.get("plan_sha256") or evidence.get("plan_file_sha256") != usage.get("file_sha256"):
        errors.append("smoke test plan hashes do not match frozen usage plan")
    plan = load_json(plan_path(model_root)) if plan_path(model_root).is_file() else {}
    guide_hashes = plan.get("source_guide_hashes") or {}
    if evidence.get("guide_paths") != plan.get("source_guide_paths") or evidence.get("guide_sha256") != guide_hashes:
        errors.append("smoke test guide evidence does not match frozen usage plan")
    checkpoint_plan = plan.get("checkpoint_loading") or {}
    if checkpoint_plan.get("variant") and evidence.get("checkpoint_variant") != checkpoint_plan.get("variant"):
        errors.append("smoke test checkpoint variant does not match frozen usage plan")
    expected_calculator = (plan.get("calculator_factory") or {}).get("class")
    if expected_calculator and evidence.get("calculator_class") != expected_calculator:
        errors.append("smoke test calculator class does not match frozen usage plan")
    expected_device = (plan.get("device_policy") or {}).get("device")
    if expected_device and evidence.get("device") != expected_device:
        errors.append("smoke test device does not match frozen usage plan")
    expected_dtype = (plan.get("dtype_policy") or {}).get("dtype")
    if expected_dtype and evidence.get("dtype") != expected_dtype:
        errors.append("smoke test dtype does not match frozen usage plan")
    expected_units = plan.get("energy_units") or {}
    if expected_units.get("energy") and evidence.get("energy_unit") != expected_units.get("energy"):
        errors.append("smoke test energy unit does not match frozen usage plan")
    if expected_units.get("force") and evidence.get("force_unit") != expected_units.get("force"):
        errors.append("smoke test force unit does not match frozen usage plan")
    checkpoint_recorded = str(evidence.get("checkpoint_path") or "")
    checkpoint_file = run_root / checkpoint_recorded
    try:
        checkpoint_file.resolve().relative_to(run_root.resolve())
    except ValueError:
        errors.append("smoke test checkpoint_path resolves outside the run")
    else:
        if not checkpoint_file.is_file():
            errors.append(f"smoke test checkpoint is missing: {checkpoint_recorded}")
        elif evidence.get("checkpoint_sha256") != sha256_file(checkpoint_file):
            errors.append("smoke test checkpoint hash does not match the recorded file")
    for field in ("stdout_path", "stderr_path"):
        recorded = str(evidence.get(field) or "")
        output_file = run_root / recorded
        try:
            output_file.resolve().relative_to(run_root.resolve())
        except ValueError:
            errors.append(f"smoke test {field} resolves outside the run: {recorded}")
        else:
            if not output_file.is_file():
                errors.append(f"smoke test {field} is missing: {recorded}")
    return {"passed": not errors, "errors": errors, "path": str(path.relative_to(run_root)).replace("\\", "/"), "evidence": evidence}


def prepare_model_script(args: argparse.Namespace) -> dict[str, Any]:
    model_root = args.model_root.resolve()
    run_root, model_key = model_context(model_root)
    usage = validate_usage_plan(model_root)
    if not usage.get("passed"):
        raise UsageError("usage plan is not valid: " + "; ".join(usage.get("errors") or []))
    preparation_errors = audit_record(model_root, require_plan=True)
    if preparation_errors:
        raise UsageError("environment preparation is not valid: " + "; ".join(preparation_errors))
    smoke = validate_smoke_test(model_root)
    if not smoke.get("passed"):
        raise UsageError("official usage smoke test is not valid: " + "; ".join(smoke.get("errors") or []))
    isolation_path = model_root / "validation" / "script_calculator_isolation.json"
    if not isolation_path.is_file() or not load_json(isolation_path).get("passed"):
        raise UsageError("calculator isolation evidence must pass before script registration")
    scripts: dict[str, Any] = {}
    for task, value in (("single_point", args.single_point_path), ("relaxation", args.relaxation_path)):
        if not value:
            continue
        path = Path(value).resolve()
        try:
            relative = str(path.relative_to(run_root)).replace("\\", "/")
            path.relative_to((model_root / "scripts").resolve())
        except ValueError as exc:
            raise UsageError(f"{task} script must be inside models/{model_key}/scripts") from exc
        if path.is_symlink() or not path.is_file():
            raise UsageError(f"{task} script must be a real regular file")
        scripts[task] = {"path": relative, "sha256": sha256_file(path)}
    if not scripts:
        raise UsageError("at least one model-specific script path is required")
    record = {
        "schema_version": SCHEMA_VERSION, "model_key": model_key, "status": "verified",
        "usage_plan_sha256": usage["plan_sha256"], "usage_plan_file_sha256": usage["file_sha256"],
        "guide_paths": usage["guide_paths"], "smoke_test_path": smoke["path"], "scripts": scripts,
        "execution_variant": frozen_model(model_root)["execution_variant"],
        "catalysis_assessment_sha256": canonical_json_sha256(frozen_model(model_root)["catalysis_assessment"]),
    }
    destination = model_root / "validation" / "script_preparation.json"
    atomic_write_json(destination, record)
    return {"model_key": model_key, "status": "verified", "path": str(destination.relative_to(run_root)).replace("\\", "/"), "scripts": scripts}


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze and validate official model-usage evidence")
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze-usage-plan")
    freeze.add_argument("model_root", type=Path)
    freeze.add_argument("--plan-source", type=Path, required=True)
    validate = sub.add_parser("validate-usage-plan")
    validate.add_argument("model_root", type=Path)
    smoke = sub.add_parser("validate-official-smoke-test")
    smoke.add_argument("model_root", type=Path)
    prepare = sub.add_parser("prepare-model-script")
    prepare.add_argument("model_root", type=Path)
    prepare.add_argument("--single-point-path")
    prepare.add_argument("--relaxation-path")
    args = parser.parse_args()
    try:
        if args.command == "freeze-usage-plan":
            result = freeze_usage_plan(args)
        elif args.command == "validate-usage-plan":
            result = validate_usage_plan(args.model_root)
        elif args.command == "validate-official-smoke-test":
            result = validate_smoke_test(args.model_root)
        else:
            result = prepare_model_script(args)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.get("passed", True) else 1
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
