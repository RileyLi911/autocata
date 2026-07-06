#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from omegaconf import OmegaConf


PROJECT_ROOT = Path(__file__).resolve().parent


def parse_args():
    parser = argparse.ArgumentParser(description="Run a multi-adsorbate reaction-network workflow.")
    parser.add_argument("--reaction-config", default="config/reaction_workflow_config.yml")
    parser.add_argument("--project-config", default="config/config.yml")
    parser.add_argument("--workflow-template", help="Override reaction_workflow.workflow_template.")
    parser.add_argument("--adsorbates", help="Comma-separated adsorbate names. Overrides reaction_workflow.adsorbates.")
    parser.add_argument("--run-dir", help="Override reaction workflow output directory.")
    parser.add_argument("--dry-run", action="store_true", help="Plan child workflow commands without running generation/MLP.")
    return parser.parse_args()


def resolve_project_path(path):
    path = Path(path)
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


def path_for_message(path):
    path = Path(path)
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def slugify(value):
    keep = []
    for char in str(value):
        if char.isalnum() or char in ["-", "_"]:
            keep.append(char)
        else:
            keep.append("_")
    return "".join(keep).strip("_") or "reaction_workflow"


def command_to_text(command):
    return " ".join(f'"{part}"' if " " in str(part) else str(part) for part in command)


def run_command(command, log_file):
    command = [str(part) for part in command]
    with open(log_file, "a", encoding="utf-8") as log:
        log.write(f"\n$ {command_to_text(command)}\n")
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
        )
        if result.stdout:
            log.write(result.stdout)
        if result.stderr:
            log.write(result.stderr)
        if result.returncode != 0:
            raise RuntimeError(f"Command failed ({result.returncode}): {command_to_text(command)}")


def read_csv(path):
    if not Path(path).is_file():
        return []
    with open(path, "r", newline="", encoding="utf-8") as fr:
        return list(csv.DictReader(fr))


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fw:
        writer = csv.DictWriter(fw, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fw:
        json.dump(payload, fw, indent=2)


def load_json(path):
    if not Path(path).is_file():
        return {}
    with open(path, "r", encoding="utf-8") as fr:
        return json.load(fr)


def make_run_dir(reaction, args):
    if args.run_dir:
        return resolve_project_path(args.run_dir)

    run_name = OmegaConf.select(reaction, "reaction_workflow.run_name", default=None)
    if run_name is None:
        adsorbates = "_".join(OmegaConf.select(reaction, "reaction_workflow.adsorbates", default=[]) or [])
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_name = f"reaction_{adsorbates}_{timestamp}"

    output_root = resolve_project_path(reaction.reaction_workflow.output_root)
    return output_root / slugify(run_name)


def selected_adsorbates(reaction, args):
    if args.adsorbates:
        return [item.strip() for item in args.adsorbates.split(",") if item.strip()]
    return list(OmegaConf.select(reaction, "reaction_workflow.adsorbates", default=[]) or [])


def load_registry(path):
    registry_path = resolve_project_path(path)
    if not registry_path.is_file():
        raise FileNotFoundError(f"Missing adsorbate registry: {path_for_message(registry_path)}")
    registry = OmegaConf.load(registry_path)
    entries = {entry.name: entry for entry in registry.adsorbates}
    return registry, entries, registry_path


def validate_adsorbates(registry, entries, adsorbates):
    if not adsorbates:
        raise ValueError("reaction_workflow.adsorbates is empty.")

    errors = []
    for adsorbate in adsorbates:
        entry = entries.get(adsorbate)
        if entry is None:
            errors.append(f"{adsorbate}: not listed in registry")
            continue
        if not OmegaConf.select(entry, "enabled", default=True):
            errors.append(f"{adsorbate}: disabled in registry")
            continue

        checkpoint_dir = resolve_project_path(registry.checkpoint_dir_template.format(adsorbate=adsorbate))
        if not checkpoint_dir.is_dir():
            errors.append(f"{adsorbate}: missing checkpoint dir {path_for_message(checkpoint_dir)}")
            continue

        for required in OmegaConf.select(registry, "required_files", default=[]) or []:
            if not (checkpoint_dir / required).is_file():
                errors.append(f"{adsorbate}: missing {required}")

    if errors:
        raise ValueError("Invalid adsorbate selection:\n" + "\n".join(errors))


def copy_if_present(source, target, config_key):
    value = OmegaConf.select(source, config_key, default=None)
    if value is not None:
        target[config_key] = OmegaConf.to_container(value, resolve=True)


def build_child_workflow_config(reaction, workflow_template, adsorbate, run_name, registry_path):
    workflow = OmegaConf.load(workflow_template)
    wf = workflow.workflow
    rw = reaction.reaction_workflow

    wf.adsorbate = adsorbate
    wf.adsorbate_registry = path_for_message(registry_path)
    wf.run_name = f"{run_name}_{adsorbate}"
    wf.output_root = "outputs/workflows"

    per_adsorbate = rw.per_adsorbate
    wf.target_success_count = int(per_adsorbate.target_success_count)
    wf.max_rounds = int(per_adsorbate.max_rounds)
    wf.generation_per_round = int(per_adsorbate.generation_per_round)

    for key in [
        "fine_tune",
        "device",
        "generation",
        "material_query",
        "filters",
        "duplicate_check",
    ]:
        value = OmegaConf.select(rw, key, default=None)
        if value is not None:
            wf[key] = OmegaConf.to_container(value, resolve=True)

    wf.keep_failed_structures = bool(OmegaConf.select(rw, "keep_failed_structures", default=False))
    if "viewer" not in wf:
        wf.viewer = {}
    wf.viewer.auto_open = False
    return workflow


def as_float(value):
    try:
        if value in [None, ""]:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def best_energy_row(rows):
    with_energy = [(as_float(row.get("E_pred")), row) for row in rows]
    with_energy = [(energy, row) for energy, row in with_energy if energy is not None]
    if not with_energy:
        return None, None
    return min(with_energy, key=lambda item: item[0])


def aggregate_materials(success_rows, adsorbates, per_material_target):
    grouped = {}
    for row in success_rows:
        material = row.get("material") or ""
        if not material:
            continue
        material_elements = row.get("material_elements") or ""
        key = (material, material_elements)
        grouped.setdefault(key, {}).setdefault(row["adsorbate"], []).append(row)

    summary_rows = []
    matrix_rows = []
    dynamic_fields = []
    for adsorbate in adsorbates:
        dynamic_fields.extend([
            f"{adsorbate}_count",
            f"{adsorbate}_best_E_pred",
            f"{adsorbate}_best_xyz_file",
        ])

    for (material, material_elements), by_adsorbate in grouped.items():
        missing = []
        best_energies = []
        total_success = 0
        matrix_row = {
            "material": material,
            "material_elements": material_elements,
            "required_adsorbate_count": len(adsorbates),
        }

        for adsorbate in adsorbates:
            rows = by_adsorbate.get(adsorbate, [])
            count = len(rows)
            total_success += count
            best_energy, best_row = best_energy_row(rows)
            if count < per_material_target:
                missing.append(adsorbate)
            if best_energy is not None:
                best_energies.append(best_energy)
            matrix_row[f"{adsorbate}_count"] = count
            matrix_row[f"{adsorbate}_best_E_pred"] = "" if best_energy is None else best_energy
            matrix_row[f"{adsorbate}_best_xyz_file"] = "" if best_row is None else best_row.get("success_xyz_file", "")

        coverage_count = len(adsorbates) - len(missing)
        all_passed = len(missing) == 0
        mean_best = sum(best_energies) / len(best_energies) if best_energies else None
        worst_best = max(best_energies) if best_energies else None

        common = {
            "material": material,
            "material_elements": material_elements,
            "coverage_count": coverage_count,
            "required_adsorbate_count": len(adsorbates),
            "all_adsorbates_passed": str(all_passed).lower(),
            "missing_adsorbates": ";".join(missing),
            "total_success": total_success,
            "worst_best_energy": "" if worst_best is None else worst_best,
            "mean_best_energy": "" if mean_best is None else mean_best,
        }
        summary_rows.append(common)
        matrix_row.update(common)
        matrix_rows.append(matrix_row)

    def sort_key(row):
        worst = as_float(row.get("worst_best_energy"))
        mean = as_float(row.get("mean_best_energy"))
        return (
            -int(row["coverage_count"]),
            worst if worst is not None else float("inf"),
            mean if mean is not None else float("inf"),
            row["material"],
        )

    summary_rows = sorted(summary_rows, key=sort_key)
    matrix_rows = sorted(matrix_rows, key=sort_key)
    return summary_rows, matrix_rows, dynamic_fields


def main():
    args = parse_args()
    reaction_config = resolve_project_path(args.reaction_config)
    project_config = resolve_project_path(args.project_config)
    reaction = OmegaConf.load(reaction_config)

    workflow_template = resolve_project_path(
        args.workflow_template or reaction.reaction_workflow.workflow_template
    )
    registry, registry_entries, registry_path = load_registry(reaction.reaction_workflow.adsorbate_registry)
    adsorbates = selected_adsorbates(reaction, args)
    validate_adsorbates(registry, registry_entries, adsorbates)

    run_dir = make_run_dir(reaction, args)
    run_dir.mkdir(parents=True, exist_ok=True)
    log_file = run_dir / "reaction_run.log"
    child_config_dir = run_dir / "configs"
    child_run_root = run_dir / "adsorbate_runs"
    child_config_dir.mkdir(parents=True, exist_ok=True)
    child_run_root.mkdir(parents=True, exist_ok=True)

    run_name = run_dir.name
    adsorbate_run_rows = []

    try:
        for adsorbate in adsorbates:
            entry = registry_entries[adsorbate]
            child_run_dir = child_run_root / slugify(adsorbate)
            child_config = build_child_workflow_config(reaction, workflow_template, adsorbate, run_name, registry_path)
            child_config_path = child_config_dir / f"workflow_{slugify(adsorbate)}.yml"
            OmegaConf.save(child_config, child_config_path)

            command = [
                sys.executable,
                "workflow_runner.py",
                "--workflow-config",
                child_config_path,
                "--project-config",
                project_config,
                "--run-dir",
                child_run_dir,
            ]
            if args.dry_run:
                command.append("--dry-run")
            run_command(command, log_file)

            report_path = child_run_dir / "workflow_report.json"
            report = load_json(report_path)
            counts = report.get("counts", {})
            outputs = report.get("outputs", {})
            adsorbate_run_rows.append(
                {
                    "adsorbate": adsorbate,
                    "formula": OmegaConf.select(entry, "formula", default=adsorbate),
                    "status": report.get("status", ""),
                    "success": counts.get("success", 0),
                    "target_success_count": counts.get("target_success_count", ""),
                    "candidates": counts.get("candidates", 0),
                    "run_dir": path_for_message(child_run_dir),
                    "workflow_report": path_for_message(report_path),
                    "success_summary_csv": outputs.get("success_summary_csv", ""),
                    "failure_summary_csv": outputs.get("failure_summary_csv", ""),
                    "viewer_index": outputs.get("viewer_index", ""),
                    "checkpoint_path": report.get("checkpoint_path", ""),
                }
            )

        all_success_rows = []
        for run_row in adsorbate_run_rows:
            success_csv = resolve_project_path(run_row["success_summary_csv"])
            for row in read_csv(success_csv):
                row.update(
                    adsorbate=run_row["adsorbate"],
                    adsorbate_formula=run_row["formula"],
                    adsorbate_run_dir=run_row["run_dir"],
                )
                all_success_rows.append(row)

        per_material_target = int(
            OmegaConf.select(
                reaction,
                "reaction_workflow.shared_material.target_success_per_adsorbate_per_material",
                default=1,
            )
        )
        material_summary, matrix_rows, dynamic_fields = aggregate_materials(
            all_success_rows,
            adsorbates,
            per_material_target,
        )
        target_shared_count = int(OmegaConf.select(reaction, "reaction_workflow.target_shared_material_count", default=1))
        shared_count = sum(1 for row in material_summary if row["all_adsorbates_passed"] == "true")

        write_csv(
            run_dir / "adsorbate_run_summary.csv",
            [
                "adsorbate",
                "formula",
                "status",
                "success",
                "target_success_count",
                "candidates",
                "run_dir",
                "workflow_report",
                "success_summary_csv",
                "failure_summary_csv",
                "viewer_index",
                "checkpoint_path",
            ],
            adsorbate_run_rows,
        )
        success_fields = [
            "adsorbate",
            "adsorbate_formula",
            "adsorbate_run_dir",
            "round",
            "xyz_file",
            "sample_id",
            "natoms",
            "E_pred",
            "F_rms",
            "F_max",
            "material",
            "material_elements",
            "adsorbate_valid",
            "passed",
            "failure_reason",
            "error",
            "success_xyz_file",
        ]
        write_csv(run_dir / "all_success_structures.csv", success_fields, all_success_rows)
        summary_fields = [
            "material",
            "material_elements",
            "coverage_count",
            "required_adsorbate_count",
            "all_adsorbates_passed",
            "missing_adsorbates",
            "total_success",
            "worst_best_energy",
            "mean_best_energy",
        ]
        write_csv(run_dir / "shared_material_summary.csv", summary_fields, material_summary)
        write_csv(run_dir / "material_adsorbate_matrix.csv", summary_fields + dynamic_fields, matrix_rows)

        status = "dry_run" if args.dry_run else ("success" if shared_count >= target_shared_count else "incomplete")
        report = {
            "status": status,
            "run_dir": path_for_message(run_dir),
            "reaction_config": path_for_message(reaction_config),
            "workflow_template": path_for_message(workflow_template),
            "project_config": path_for_message(project_config),
            "adsorbate_registry": path_for_message(registry_path),
            "adsorbates": adsorbates,
            "counts": {
                "adsorbates": len(adsorbates),
                "success_structures": len(all_success_rows),
                "shared_materials": shared_count,
                "target_shared_material_count": target_shared_count,
            },
            "outputs": {
                "adsorbate_run_summary_csv": path_for_message(run_dir / "adsorbate_run_summary.csv"),
                "all_success_structures_csv": path_for_message(run_dir / "all_success_structures.csv"),
                "shared_material_summary_csv": path_for_message(run_dir / "shared_material_summary.csv"),
                "material_adsorbate_matrix_csv": path_for_message(run_dir / "material_adsorbate_matrix.csv"),
                "reaction_run_log": path_for_message(log_file),
            },
        }
        report_path = run_dir / "reaction_workflow_report.json"
        write_json(report_path, report)
    except Exception as exc:
        report = {
            "status": "failed",
            "run_dir": path_for_message(run_dir),
            "reaction_config": path_for_message(reaction_config),
            "error": str(exc),
        }
        report_path = run_dir / "reaction_workflow_report.json"
        write_json(report_path, report)
        raise

    print(f"[done] status={report['status']}")
    print(f"[done] shared_materials={report['counts']['shared_materials']}/{report['counts']['target_shared_material_count']}")
    print(f"[done] report={path_for_message(report_path)}")


if __name__ == "__main__":
    main()
