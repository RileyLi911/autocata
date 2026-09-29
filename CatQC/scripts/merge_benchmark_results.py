from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from common import (
    as_bool, as_float, assess_result_row, atomic_write_csv, atomic_write_json,
    file_evidence, frame_paths, load_json, metric_values, read_csv,
    read_extxyz_structure, sha256_file, validate_poscar, write_poscar,
)
from validate_run import validate


LONG_COLUMNS = [
    "task", "model_key", "model_name", "selection_index", "snapshot_rank", "structure_index",
    "slab_composition", "dft_adsorption_energy_eV", "model_adsorption_energy_eV",
    "error_eV", "abs_error_eV", "converged", "dissociation_status",
    "reliability_category", "invalid_reason", "dissociation_reason", "broken_bond_count",
    "max_monitored_bond_distance_A", "final_adsorbate_slab_distance_A",
    "initial_adsorbate_slab_distance_A", "adsorbate_slab_distance_increase_A",
    "slab_max_displacement_A", "fixed_atom_indices", "fixed_atom_max_displacement_A",
    "fixed_atom_displacement_tolerance_A", "fixed_atom_displacement_violation",
    "min_pair_covalent_ratio", "source_type", "status", "source_status",
    "error_message", "scientific_exception_ids",
]
METRIC_COLUMNS = [
    "task", "model_key", "model_name", "selection_index", "snapshot_rank", "n_expected", "n_valid",
    "n_failed", "n_missing", "coverage_fraction", "n_intact", "n_dissociated",
    "n_integrity_unknown", "mae_eV", "rmse_eV", "mean_error_eV",
    "median_abs_error_eV", "max_abs_error_eV", "spearman", "min_pred_eV", "max_pred_eV",
]
FAILURE_COLUMNS = [
    "task", "model_key", "model_name", "selection_index", "snapshot_rank", "structure_index",
    "slab_composition", "status", "source_status", "error_source", "error_message",
]
DISSOCIATION_COLUMNS = [
    "model_key", "model_name", "selection_index", "snapshot_rank", "structure_index", "slab_composition",
    "converged", "dissociation_status", "reliability_category", "invalid_reason",
    "dissociation_reason", "broken_bond_count", "max_monitored_bond_distance_A",
    "final_adsorbate_slab_distance_A", "initial_adsorbate_slab_distance_A",
    "adsorbate_slab_distance_increase_A", "slab_max_displacement_A",
    "fixed_atom_indices", "fixed_atom_max_displacement_A",
    "fixed_atom_displacement_tolerance_A", "fixed_atom_displacement_violation",
    "min_pair_covalent_ratio", "monitored_bond_distances_A_json",
    "integrity_check_method", "source_type", "poscar_path",
]
INTACT_COLUMNS = [
    "model_key", "model_name", "selection_index", "snapshot_rank", "n_expected", "n_valid_intact",
    "n_excluded_dissociated", "n_excluded_integrity_unknown", "excluded_structure_indices",
    "mae_eV", "rmse_eV", "mean_error_eV", "median_abs_error_eV", "max_abs_error_eV",
    "spearman", "min_pred_eV", "max_pred_eV",
]
POSCAR_INDEX_COLUMNS = [
    "model_key", "structure_index", "status", "converged",
    "source_frame_manifest", "source_structure_sha256", "poscar_path",
    "poscar_sha256", "dissociation_status", "error_message",
]


def load_reference(path: Path, expected: int, *, allow_partial: bool = False) -> dict[int, dict[str, str]]:
    rows = read_csv(path)
    result: dict[int, dict[str, str]] = {}
    for row in rows:
        index = int(row["structure_index"])
        if index in result:
            raise ValueError(f"duplicate DFT structure_index {index}")
        if as_float(row.get("dft_adsorption_energy_eV")) is None:
            raise ValueError(f"nonfinite DFT energy at structure_index {index}")
        result[index] = row
    if any(index < 0 or index >= expected for index in result):
        raise ValueError(f"DFT reference indices must be within 0..{expected - 1}")
    if not allow_partial and sorted(result) != list(range(expected)):
        raise ValueError(f"DFT reference indices must be contiguous 0..{expected - 1}")
    if allow_partial and not result:
        raise ValueError("approved partial DFT reference must contain at least one valid frame")
    return result


def task_file(root: Path, model_key: str, task: str) -> Path:
    name = "adsorption_energy_SP.csv" if task == "single_point" else "adsorption_energy_Relax.csv"
    return root / "models" / model_key / "results" / name


def export_relax_poscars(root: Path, model_key: str, rows: dict[int, dict[str, str]], expected: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index in range(expected):
        row = rows.get(index) or {}
        status = str(row.get("status") or "missing")
        converged = row.get("converged", "")
        frame = frame_paths(root, model_key, "relaxation", index)
        manifest_path = frame["manifest"]
        poscar_path = root / "summary" / "structures" / "Relax" / model_key / f"{index:08d}" / "POSCAR"
        record: dict[str, Any] = {
            "model_key": model_key,
            "structure_index": index,
            "status": status,
            "converged": converged,
            "source_frame_manifest": str(manifest_path.relative_to(root)).replace("\\", "/") if manifest_path.is_file() else "",
            "source_structure_sha256": "",
            "poscar_path": "",
            "poscar_sha256": "",
            "dissociation_status": row.get("dissociation_status", ""),
            "error_message": row.get("error_message", ""),
        }
        valid = status.strip().lower() in {"success", "completed", "complete"} and as_bool(converged) is True
        if not valid:
            if poscar_path.is_file():
                raise ValueError(f"stale POSCAR exists for non-success Relax frame: {poscar_path}")
            records.append(record)
            continue
        if not manifest_path.is_file():
            raise ValueError(f"missing frame manifest for Relax frame {model_key}/{index}")
        manifest = load_json(manifest_path)
        evidence = (manifest.get("outputs") or {}).get("relaxed_structure") or {}
        source = root / str(evidence.get("path") or "")
        if not source.is_file():
            raise ValueError(f"missing per-frame relaxed structure for {model_key}/{index}")
        if evidence.get("sha256") != sha256_file(source):
            raise ValueError(f"per-frame relaxed structure hash mismatch for {model_key}/{index}")
        structure = read_extxyz_structure(source)
        poscar_path.parent.mkdir(parents=True, exist_ok=True)
        write_poscar(structure, poscar_path, f"{model_key} structure_index={index}")
        validate_poscar(poscar_path)
        record.update({
            "source_structure_sha256": sha256_file(source),
            "poscar_path": str(poscar_path.relative_to(root)).replace("\\", "/"),
            "poscar_sha256": sha256_file(poscar_path),
        })
        records.append(record)
    return records


def merge(root: Path) -> dict[str, Any]:
    validation = validate(root)
    if validation["errors"]:
        raise ValueError("run validation failed before merge: " + "; ".join(validation["errors"]))
    config = load_json(root / "benchmark_config.json")
    worklist = load_json(root / "models_manifest.json")
    run_state = load_json(root / "run_state.json")
    if run_state.get("reconciliation_required") or run_state.get("active_jobs"):
        raise ValueError("merge is blocked by active MLP jobs or pending reconciliation")
    dft_state = run_state.get("dft_reference")
    if dft_state:
        allowed_dft = {"provided_ready"} if dft_state.get("mode") == "provided" else {"complete", "partial_approved"}
        if dft_state.get("status") not in allowed_dft:
            raise ValueError("DFT reference branch has not reached the merge barrier")
    requested_summaries = [task for model in (run_state.get("models") or {}).values() for task in (model.get("tasks") or {}).values() if task.get("status") != "not_requested"]
    if requested_summaries and any(task.get("status") != "complete" for task in requested_summaries):
        raise ValueError("all requested MLP task checkpoints must be complete before merge")
    expected = int(config["frame_count"])
    allow_partial_dft = (run_state.get("dft_reference") or {}).get("status") == "partial_approved"
    references = load_reference(root / "summary" / "dft_reference.csv", expected, allow_partial=allow_partial_dft)
    tasks = config["benchmark_request"]["requested_tasks"]
    result_validation = config.get("result_validation") or {}
    relaxation = config.get("relaxation") or {}
    long_rows: list[dict[str, Any]] = []
    metric_rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    dissociation_rows: list[dict[str, Any]] = []
    intact_rows: list[dict[str, Any]] = []
    poscar_rows: list[dict[str, Any]] = []

    for model in worklist["models"]:
        model_key = model["model_key"]
        model_name = model["model_name"]
        selection_index = model["selection_index"]
        rank = model["snapshot_rank"]
        for task_name in tasks:
            task = "SP" if task_name == "single_point" else "Relax"
            path = task_file(root, model_key, task_name)
            raw_by_index: dict[int, dict[str, str]] = {}
            if path.is_file():
                for row in read_csv(path):
                    index = int(row["structure_index"])
                    if index in raw_by_index:
                        raise ValueError(f"duplicate index {index} in {path}")
                    raw_by_index[index] = row
            poscar_by_index: dict[int, dict[str, Any]] = {}
            if task == "Relax":
                poscar_records = export_relax_poscars(root, model_key, raw_by_index, expected)
                poscar_rows.extend(poscar_records)
                poscar_by_index = {int(item["structure_index"]): item for item in poscar_records}
            predictions: list[float] = []
            dft_values: list[float] = []
            counts = defaultdict(int)
            intact_predictions: list[float] = []
            intact_dft: list[float] = []
            excluded: list[int] = []

            for index in range(expected):
                reference = references.get(index)
                if reference is None:
                    counts["missing"] += 1
                    failures.append({
                        "task": task, "model_key": model_key, "model_name": model_name,
                        "selection_index": selection_index, "snapshot_rank": rank, "structure_index": index,
                        "slab_composition": "", "status": "missing", "source_status": "missing",
                        "error_source": "dft_reference", "error_message": "no valid DFT reference for this frame",
                    })
                    continue
                row = raw_by_index.get(index)
                assessment = None if row is None else assess_result_row(
                    row, task, result_validation, relaxation
                )
                status = "missing" if assessment is None else assessment["effective_status"]
                source_status = "missing" if assessment is None else assessment["source_status"]
                pred = None if assessment is None else assessment["energy"]
                dft = float(reference["dft_adsorption_energy_eV"])
                valid = False if assessment is None else assessment["valid"]
                recorded_error = (row or {}).get("error_message", "missing result row" if row is None else "")
                validity_error = "; ".join(assessment["reasons"]) if assessment else ""
                error_message = "; ".join(part for part in (recorded_error, validity_error) if part)
                integrity = "not_applicable" if task == "SP" else str((row or {}).get("dissociation_status") or "unknown").lower()
                if task == "Relax" and integrity not in {"intact", "dissociated", "unknown"}:
                    integrity = "unknown"
                counts["valid" if valid else ("missing" if status == "missing" else "failed")] += 1
                if task == "Relax":
                    counts[integrity] += 1
                error = pred - dft if valid and pred is not None else None
                long = {
                    "task": task, "model_key": model_key, "model_name": model_name,
                    "selection_index": selection_index, "snapshot_rank": rank, "structure_index": index,
                    "slab_composition": reference.get("slab_composition", ""),
                    "dft_adsorption_energy_eV": dft,
                    "model_adsorption_energy_eV": pred if valid else "",
                    "error_eV": error if error is not None else "",
                    "abs_error_eV": abs(error) if error is not None else "",
                    "converged": (row or {}).get("converged", "not_applicable" if task == "SP" else ""),
                    "dissociation_status": integrity,
                    "reliability_category": (row or {}).get("reliability_category", ""),
                    "invalid_reason": (row or {}).get("invalid_reason", ""),
                    "dissociation_reason": (row or {}).get("dissociation_reason", ""),
                    "broken_bond_count": (row or {}).get("broken_bond_count", ""),
                    "max_monitored_bond_distance_A": (row or {}).get("max_monitored_bond_distance_A", ""),
                    "final_adsorbate_slab_distance_A": (row or {}).get("final_adsorbate_slab_distance_A", ""),
                    "initial_adsorbate_slab_distance_A": (row or {}).get("initial_adsorbate_slab_distance_A", ""),
                    "adsorbate_slab_distance_increase_A": (row or {}).get("adsorbate_slab_distance_increase_A", ""),
                    "slab_max_displacement_A": (row or {}).get("slab_max_displacement_A", ""),
                    "fixed_atom_indices": json.dumps((row or {}).get("fixed_atom_indices", [])),
                    "fixed_atom_max_displacement_A": (row or {}).get("fixed_atom_max_displacement_A", ""),
                    "fixed_atom_displacement_tolerance_A": (row or {}).get("fixed_atom_displacement_tolerance_A", ""),
                    "fixed_atom_displacement_violation": (row or {}).get("fixed_atom_displacement_violation", ""),
                    "min_pair_covalent_ratio": (row or {}).get("min_pair_covalent_ratio", ""),
                    "source_type": (row or {}).get("source_type", "mlp"),
                    "status": status, "source_status": source_status,
                    "error_message": error_message,
                    "scientific_exception_ids": (row or {}).get("scientific_exception_ids", ""),
                }
                long_rows.append(long)
                if valid and pred is not None:
                    predictions.append(pred)
                    dft_values.append(dft)
                else:
                    failures.append({
                        "task": task, "model_key": model_key, "model_name": model_name,
                        "selection_index": selection_index, "snapshot_rank": rank, "structure_index": index,
                        "slab_composition": reference.get("slab_composition", ""),
                        "status": status, "source_status": source_status,
                        "error_source": "result_validation" if assessment and assessment["reasons"] else ("result_csv" if row else "coverage"),
                        "error_message": long["error_message"],
                    })
                if task == "Relax":
                    dissociation_rows.append({
                        "model_key": model_key, "model_name": model_name,
                        "selection_index": selection_index, "snapshot_rank": rank,
                        "structure_index": index, "slab_composition": reference.get("slab_composition", ""),
                        "converged": (row or {}).get("converged", ""), "dissociation_status": integrity,
                        "reliability_category": (row or {}).get("reliability_category", ""),
                        "invalid_reason": (row or {}).get("invalid_reason", ""),
                        "dissociation_reason": (row or {}).get("dissociation_reason", ""),
                        "broken_bond_count": (row or {}).get("broken_bond_count", ""),
                        "max_monitored_bond_distance_A": (row or {}).get("max_monitored_bond_distance_A", ""),
                        "final_adsorbate_slab_distance_A": (row or {}).get("final_adsorbate_slab_distance_A", ""),
                        "initial_adsorbate_slab_distance_A": (row or {}).get("initial_adsorbate_slab_distance_A", ""),
                        "adsorbate_slab_distance_increase_A": (row or {}).get("adsorbate_slab_distance_increase_A", ""),
                        "slab_max_displacement_A": (row or {}).get("slab_max_displacement_A", ""),
                        "fixed_atom_indices": json.dumps((row or {}).get("fixed_atom_indices", [])),
                        "fixed_atom_max_displacement_A": (row or {}).get("fixed_atom_max_displacement_A", ""),
                        "fixed_atom_displacement_tolerance_A": (row or {}).get("fixed_atom_displacement_tolerance_A", ""),
                        "fixed_atom_displacement_violation": (row or {}).get("fixed_atom_displacement_violation", ""),
                        "min_pair_covalent_ratio": (row or {}).get("min_pair_covalent_ratio", ""),
                        "monitored_bond_distances_A_json": (row or {}).get("monitored_bond_distances_A_json", ""),
                        "integrity_check_method": (row or {}).get("integrity_check_method", ""),
                        "source_type": (row or {}).get("source_type", "mlp"),
                        "poscar_path": (poscar_by_index.get(index) or {}).get("poscar_path", ""),
                    })
                    if valid and integrity == "intact" and pred is not None:
                        intact_predictions.append(pred)
                        intact_dft.append(dft)
                    elif valid and integrity in {"dissociated", "unknown"}:
                        excluded.append(index)

            values = metric_values(predictions, dft_values)
            metric_rows.append({
                "task": task, "model_key": model_key, "model_name": model_name,
                "selection_index": selection_index, "snapshot_rank": rank,
                "n_expected": expected, "n_valid": counts["valid"],
                "n_failed": counts["failed"], "n_missing": counts["missing"],
                "coverage_fraction": counts["valid"] / expected,
                "n_intact": counts["intact"] if task == "Relax" else 0,
                "n_dissociated": counts["dissociated"] if task == "Relax" else 0,
                "n_integrity_unknown": counts["unknown"] if task == "Relax" else 0,
                **values,
            })
            if task == "Relax":
                intact_values = metric_values(intact_predictions, intact_dft)
                intact_rows.append({
                    "model_key": model_key, "model_name": model_name,
                    "selection_index": selection_index, "snapshot_rank": rank,
                    "n_expected": expected, "n_valid_intact": len(intact_predictions),
                    "n_excluded_dissociated": counts["dissociated"],
                    "n_excluded_integrity_unknown": counts["unknown"],
                    "excluded_structure_indices": json.dumps(excluded),
                    **intact_values,
                })

    summary = root / "summary"
    atomic_write_csv(summary / "benchmark_predictions_long.csv", LONG_COLUMNS, long_rows)
    atomic_write_csv(summary / "benchmark_metrics_by_model.csv", METRIC_COLUMNS, metric_rows)
    atomic_write_csv(summary / "benchmark_missing_or_failed_frames.csv", FAILURE_COLUMNS, failures)
    if "relaxation" in tasks:
        atomic_write_csv(summary / "benchmark_dissociation_records.csv", DISSOCIATION_COLUMNS, dissociation_rows)
        atomic_write_csv(summary / "benchmark_metrics_relax_intact_only.csv", INTACT_COLUMNS, intact_rows)
        atomic_write_csv(summary / "relaxed_structure_index.csv", POSCAR_INDEX_COLUMNS, poscar_rows)
    report = {
        "schema_version": 4,
        "dft_reference_mode": (run_state.get("dft_reference") or {}).get("mode", "provided"),
        "dft_reference_status": (run_state.get("dft_reference") or {}).get("status", "legacy_provided"),
        "dft_valid_frames": len(references),
        "dft_expected_frames": expected,
        "dft_coverage_fraction": len(references) / expected,
        "n_rows_long": len(long_rows),
        "n_metric_rows": len(metric_rows),
        "n_failure_rows": len(failures),
        "n_dissociation_rows": len(dissociation_rows),
        "metric_source": "summary/benchmark_predictions_long.csv",
        "status": "complete" if not failures else "complete_with_visible_failures",
    }
    atomic_write_json(summary / "benchmark_merge_report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("benchmark_root", type=Path)
    args = parser.parse_args()
    try:
        report = merge(args.benchmark_root.resolve())
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
