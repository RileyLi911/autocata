from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

from common import as_bool, as_float, atomic_write_json, load_json, metric_values, read_csv, sha256_file, validate_poscar
from model_usage_control import validate_smoke_test, validate_usage_plan
from validate_run import validate
from model_selection_report import validate_report


REQUIRED_SUMMARY = [
    "dft_reference.csv",
    "benchmark_predictions_long.csv",
    "benchmark_metrics_by_model.csv",
    "benchmark_missing_or_failed_frames.csv",
    "benchmark_merge_report.json",
]


def close_enough(left: float | None, right: float | None, tolerance: float = 1e-10) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return math.isclose(left, right, rel_tol=tolerance, abs_tol=tolerance)


def audit(root: Path) -> dict:
    validation = validate(root)
    errors = list(validation["errors"])
    warnings = list(validation["warnings"])
    report_validation = validate_report(root, final=True)
    errors.extend(f"model selection report: {error}" for error in report_validation["errors"])
    summary = root / "summary"
    for name in REQUIRED_SUMMARY:
        if not (summary / name).is_file():
            errors.append(f"missing summary/{name}")
    config_path = root / "benchmark_config.json"
    tasks = []
    config = None
    if config_path.is_file():
        tasks = load_json(config_path)["benchmark_request"]["requested_tasks"]
        config = load_json(config_path)
    if "relaxation" in tasks:
        for name in ("benchmark_dissociation_records.csv", "benchmark_metrics_relax_intact_only.csv"):
            if not (summary / name).is_file():
                errors.append(f"missing summary/{name}")
        poscar_index_path = summary / "relaxed_structure_index.csv"
        if not poscar_index_path.is_file():
            errors.append("missing summary/relaxed_structure_index.csv")
        else:
            seen_poscar_keys: set[tuple[str, int]] = set()
            for row in read_csv(poscar_index_path):
                key = (str(row.get("model_key") or ""), int(row.get("structure_index") or -1))
                if key in seen_poscar_keys:
                    errors.append(f"duplicate POSCAR index record: {key[0]}/{key[1]}")
                seen_poscar_keys.add(key)
                status = str(row.get("status") or "").strip().lower()
                if status in {"success", "completed", "complete"} and as_bool(row.get("converged")) is True:
                    poscar = root / str(row.get("poscar_path") or "")
                    source_manifest = root / str(row.get("source_frame_manifest") or "")
                    if not poscar.is_file():
                        errors.append(f"missing final POSCAR: {row.get('poscar_path')}")
                    else:
                        try:
                            validate_poscar(poscar)
                        except Exception as exc:
                            errors.append(f"invalid final POSCAR {row.get('poscar_path')}: {exc}")
                        if row.get("poscar_sha256") != sha256_file(poscar):
                            errors.append(f"final POSCAR hash mismatch: {row.get('poscar_path')}")
                    if not source_manifest.is_file():
                        errors.append(f"missing POSCAR source manifest: {row.get('source_frame_manifest')}")
                    if not row.get("source_structure_sha256"):
                        errors.append(f"missing source structure hash for POSCAR {key[0]}/{key[1]}")
                elif row.get("poscar_path"):
                    errors.append(f"non-success Relax frame must not have a POSCAR: {key[0]}/{key[1]}")

    worklist_path = root / "models_manifest.json"
    if worklist_path.is_file() and config:
        worklist = load_json(worklist_path)
        for model in worklist.get("models", []):
            model_root = root / "models" / model["model_key"]
            model_config_path = model_root / "model_config.json"
            checkpoint_manifest_path = model_root / "checkpoints" / "checkpoint_manifest.json"
            isolation_path = model_root / "validation" / "script_calculator_isolation.json"
            for path in (model_config_path, checkpoint_manifest_path, isolation_path):
                if not path.is_file():
                    errors.append(f"missing required model artifact: {path.relative_to(root)}")
            usage = validate_usage_plan(model_root)
            if not usage.get("passed"):
                errors.extend(f"model usage validation {model['model_key']}: {error}" for error in usage.get("errors") or [])
            smoke = validate_smoke_test(model_root)
            if not smoke.get("passed"):
                errors.extend(f"model usage smoke-test validation {model['model_key']}: {error}" for error in smoke.get("errors") or [])
            preparation_path = model_root / "validation" / "script_preparation.json"
            if not preparation_path.is_file() or load_json(preparation_path).get("status") != "verified":
                errors.append(f"missing or unverified script_preparation.json for {model['model_key']}")
            if model_config_path.is_file():
                model_config = load_json(model_config_path)
                scripts = model_config.get("scripts") or {}
                for task_name, field, hash_field in (
                    ("single_point", "single_point_path", "single_point_sha256"),
                    ("relaxation", "relaxation_path", "relaxation_sha256"),
                ):
                    if task_name not in tasks:
                        continue
                    relative = scripts.get(field)
                    script_path = root / str(relative or "")
                    if not relative or not script_path.is_file():
                        errors.append(f"missing model-owned {task_name} script for {model['model_key']}")
                    elif scripts.get(hash_field) != sha256_file(script_path):
                        errors.append(f"script hash mismatch for {model['model_key']} {task_name}")
            if checkpoint_manifest_path.is_file():
                checkpoint_manifest = load_json(checkpoint_manifest_path)
                for item in checkpoint_manifest.get("files", []):
                    path = root / str(item.get("path", ""))
                    if not path.is_file():
                        errors.append(f"missing checkpoint file: {item.get('path')}")
                    elif path.stat().st_size != item.get("size_bytes") or sha256_file(path) != item.get("sha256"):
                        errors.append(f"checkpoint verification failed: {item.get('path')}")
            if isolation_path.is_file() and not load_json(isolation_path).get("passed", False):
                errors.append(f"calculator isolation did not pass for {model['model_key']}")

    reproduced = 0
    if (summary / "benchmark_predictions_long.csv").is_file() and (summary / "benchmark_metrics_by_model.csv").is_file():
        groups: dict[tuple[str, str], tuple[list[float], list[float]]] = defaultdict(lambda: ([], []))
        for row in read_csv(summary / "benchmark_predictions_long.csv"):
            pred = as_float(row.get("model_adsorption_energy_eV"))
            ref = as_float(row.get("dft_adsorption_energy_eV"))
            if pred is not None and ref is not None:
                predictions, references = groups[(row["task"], row["model_key"])]
                predictions.append(pred)
                references.append(ref)
        metrics = read_csv(summary / "benchmark_metrics_by_model.csv")
        for row in metrics:
            key = (row["task"], row["model_key"])
            predictions, references = groups.get(key, ([], []))
            expected = metric_values(predictions, references)
            for column in ("mae_eV", "rmse_eV", "mean_error_eV", "median_abs_error_eV", "max_abs_error_eV", "spearman"):
                actual = as_float(row.get(column))
                if not close_enough(actual, expected[column]):
                    errors.append(f"metric drift for {key[0]}/{key[1]} {column}: table={actual}, reproduced={expected[column]}")
            reproduced += 1

    manifest_path = root / "run_manifest.json"
    if manifest_path.is_file():
        manifest = load_json(manifest_path)
        if "dft_reference_mode" in (manifest.get("request") or {}):
            state = load_json(root / "run_state.json") if (root / "run_state.json").is_file() else {}
            dft = state.get("dft_reference") or {}
            allowed = {"provided_ready"} if manifest["request"]["dft_reference_mode"] == "provided" else {"complete", "partial_approved"}
            if dft.get("status") not in allowed:
                errors.append("DFT reference branch is not ready for completion")
        if manifest.get("status") == "complete" and errors:
            errors.append("manifest is complete although completion audit fails")

    report = {
        "schema_version": 4,
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "metric_groups_reproduced": reproduced,
        "model_selection_report_passed": report_validation["passed"],
    }
    atomic_write_json(summary / "completion_audit.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("benchmark_root", type=Path)
    args = parser.parse_args()
    try:
        result = audit(args.benchmark_root.resolve())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
