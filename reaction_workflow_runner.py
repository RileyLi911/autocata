#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import json
import shutil
import subprocess
import sys
from collections import Counter
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


def run_command(command, log_file, dry_run=False):
    command = [str(part) for part in command]
    with open(log_file, "a", encoding="utf-8") as log:
        log.write(f"\n$ {command_to_text(command)}\n")
        if dry_run:
            log.write("[dry-run] skipped\n")
            return

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


def has_tokenizer(path):
    path = Path(path)
    return (path / "tokenizer.json").is_file() or (path / "vocab.txt").is_file()


def adsorbate_context(registry, registry_entries, adsorbate):
    entry = registry_entries[adsorbate]
    checkpoint_path = resolve_project_path(registry.checkpoint_dir_template.format(adsorbate=adsorbate))
    return {
        "adsorbate": adsorbate,
        "formula": OmegaConf.select(entry, "formula", default=adsorbate),
        "checkpoint_path": checkpoint_path,
        "tokenizer_path": checkpoint_path if has_tokenizer(checkpoint_path) else None,
    }


def normalize_material(material):
    elements = [item for item in str(material).split("-") if item]
    return "-".join(sorted(elements))


def material_matches(row, query):
    mode = OmegaConf.select(query, "mode", default="any")
    if mode == "any":
        return True

    row_material = normalize_material(row.get("material", ""))
    row_elements = set(filter(None, row.get("material_elements", "").split(";")))
    query_elements = set(OmegaConf.select(query, "elements", default=[]) or [])

    if mode == "exact":
        target = OmegaConf.select(query, "material", default="")
        return row_material == normalize_material(target)
    if mode == "contains":
        return query_elements.issubset(row_elements)
    if mode == "any_contains":
        return bool(query_elements.intersection(row_elements))

    raise ValueError(f"Unsupported material_query.mode: {mode}")


def metadata_failure_reason(row, reaction):
    if row.get("error"):
        return "parse_failed"
    if row.get("adsorbate_valid") != "true":
        return "wrong_adsorbate"
    if not material_matches(row, reaction.reaction_workflow.material_query):
        return "wrong_material"
    if not row.get("material"):
        return "missing_material"
    return ""


def count_bad_samples(path):
    if not Path(path).is_file():
        return 0
    with open(path, "r", encoding="utf-8") as fr:
        return sum(1 for line in fr if line.strip())


def file_fingerprint(path):
    return Path(path).read_bytes()


def evaluate_mlp_row(row, reaction, seen_fingerprints):
    rw = reaction.reaction_workflow
    require_adsorbate = OmegaConf.select(rw, "filters.require_adsorbate_valid", default=True)
    energy_gt = OmegaConf.select(rw, "filters.mlp_energy_gt", default=None)
    energy_lt = OmegaConf.select(rw, "filters.mlp_energy_lt", default=None)
    duplicate_check = OmegaConf.select(rw, "duplicate_check.enabled", default=False)

    if row.get("error"):
        return False, "mlp_error"
    if require_adsorbate and row.get("adsorbate_valid") != "true":
        return False, "wrong_adsorbate"
    if not material_matches(row, rw.material_query):
        return False, "wrong_material"
    if row.get("E_pred") == "":
        return False, "mlp_error"

    energy = float(row["E_pred"])
    if energy_gt is not None and energy <= float(energy_gt):
        return False, "mlp_energy_below_min"
    if energy_lt is not None and energy >= float(energy_lt):
        return False, "mlp_energy_above_max"

    if duplicate_check:
        xyz_path = resolve_project_path(row["xyz_file"])
        fingerprint = file_fingerprint(xyz_path)
        if fingerprint in seen_fingerprints:
            return False, "duplicate"
        seen_fingerprints.add(fingerprint)

    return True, ""


def build_material_first_generate_command(project_config, reaction, adsorbate, round_dir, context):
    rw = reaction.reaction_workflow
    generation = rw.generation
    generated_dir = round_dir / "generated"
    name = f"generated_{adsorbate}_{round_dir.name}"

    command = [
        sys.executable,
        "script/generate.py",
        "--config",
        project_config,
        "--adsorbate",
        adsorbate,
        "--save-path",
        generated_dir,
        "--name",
        name,
        "--n-generation",
        rw.per_adsorbate.generation_per_round,
        "--device",
        rw.device.generation,
    ]
    command.extend(["--ckpt-path", context["checkpoint_path"]])
    if context.get("tokenizer_path") is not None:
        command.extend(["--tokenizer-path", context["tokenizer_path"]])

    for cli_name, config_name in [
        ("--batch-size", "batch_size"),
        ("--max-length", "max_length"),
        ("--top-k", "top_k"),
        ("--top-p", "top_p"),
        ("--temperature", "temperature"),
    ]:
        value = OmegaConf.select(generation, config_name, default=None)
        if value is not None:
            command.extend([cli_name, value])

    return command, generated_dir / f"{name}.pkl"


def run_metadata_round(project_config, reaction, adsorbate, round_dir, context, log_file, dry_run):
    round_dir.mkdir(parents=True, exist_ok=True)
    generate_command, pkl_path = build_material_first_generate_command(
        project_config,
        reaction,
        adsorbate,
        round_dir,
        context,
    )
    analysis_dir = round_dir / "analysis"

    run_command(generate_command, log_file, dry_run=dry_run)
    decode_command = [
        sys.executable,
        "script/ASE_check_manual.py",
        "--config",
        project_config,
        "--adsorbate",
        adsorbate,
        "--pkl-path",
        pkl_path,
        "--output-dir",
        analysis_dir,
    ]
    if context.get("tokenizer_path") is not None:
        decode_command.extend(["--tokenizer-path", context["tokenizer_path"]])
    run_command(decode_command, log_file, dry_run=dry_run)

    run_command(
        [
            sys.executable,
            "script/index_xyz_metadata.py",
            "--config",
            project_config,
            "--adsorbate",
            adsorbate,
            "--adsorbate-formula",
            context.get("formula", adsorbate),
            "--xyz-dir",
            analysis_dir / "xyz",
            "--output-dir",
            analysis_dir,
        ],
        log_file,
        dry_run=dry_run,
    )

    return {
        "round_dir": round_dir,
        "analysis_dir": analysis_dir,
        "metadata_csv": analysis_dir / "xyz_metadata.csv",
        "bad_samples": analysis_dir / "bad_samples.txt",
    }


def aggregate_pre_mlp_materials(candidate_rows, adsorbates, min_per_adsorbate):
    grouped = {}
    for row in candidate_rows:
        if row.get("pre_mlp_passed") != "true":
            continue
        material = row.get("material") or ""
        if not material:
            continue
        key = (material, row.get("material_elements") or "")
        grouped.setdefault(key, {}).setdefault(row["adsorbate"], []).append(row)

    summary_rows = []
    matrix_rows = []
    dynamic_fields = []
    for adsorbate in adsorbates:
        dynamic_fields.extend([
            f"{adsorbate}_candidate_count",
            f"{adsorbate}_example_xyz_file",
        ])

    for (material, material_elements), by_adsorbate in grouped.items():
        missing = []
        total_candidates = 0
        matrix_row = {
            "material": material,
            "material_elements": material_elements,
            "required_adsorbate_count": len(adsorbates),
        }

        for adsorbate in adsorbates:
            rows = by_adsorbate.get(adsorbate, [])
            count = len(rows)
            total_candidates += count
            if count < min_per_adsorbate:
                missing.append(adsorbate)
            matrix_row[f"{adsorbate}_candidate_count"] = count
            matrix_row[f"{adsorbate}_example_xyz_file"] = rows[0].get("xyz_file", "") if rows else ""

        coverage_count = len(adsorbates) - len(missing)
        all_present = len(missing) == 0
        common = {
            "material": material,
            "material_elements": material_elements,
            "coverage_count": coverage_count,
            "required_adsorbate_count": len(adsorbates),
            "all_adsorbates_present": str(all_present).lower(),
            "missing_adsorbates": ";".join(missing),
            "total_pre_mlp_candidates": total_candidates,
        }
        summary_rows.append(common)
        matrix_row.update(common)
        matrix_rows.append(matrix_row)

    def sort_key(row):
        return (
            -int(row["coverage_count"]),
            -int(row["total_pre_mlp_candidates"]),
            row["material"],
        )

    summary_rows = sorted(summary_rows, key=sort_key)
    matrix_rows = sorted(matrix_rows, key=sort_key)
    return summary_rows, matrix_rows, dynamic_fields


def selected_materials_for_mlp(material_summary, reaction):
    selected = [
        row["material"]
        for row in material_summary
        if row.get("all_adsorbates_present") == "true"
    ]
    max_materials = OmegaConf.select(
        reaction,
        "reaction_workflow.material_first.max_shared_materials_for_mlp",
        default=None,
    )
    if max_materials is not None:
        selected = selected[: int(max_materials)]
    return selected


def select_rows_for_targeted_mlp(candidate_rows, materials, reaction):
    selected_materials = set(materials)
    max_per_material = OmegaConf.select(
        reaction,
        "reaction_workflow.material_first.max_structures_per_adsorbate_per_material",
        default=None,
    )
    grouped = {}
    for row in candidate_rows:
        if row.get("pre_mlp_passed") != "true":
            continue
        if row.get("material") not in selected_materials:
            continue
        key = (row["material"], row["adsorbate"])
        grouped.setdefault(key, []).append(row)

    selected_rows = []
    for key in sorted(grouped):
        rows = sorted(grouped[key], key=lambda row: (row.get("round", ""), row.get("sample_id", "")))
        if max_per_material is not None:
            rows = rows[: int(max_per_material)]
        selected_rows.extend(rows)
    return selected_rows


def prepare_targeted_mlp_inputs(run_dir, selected_rows):
    input_root = run_dir / "targeted_mlp_inputs"
    lookup = {}
    by_adsorbate = {}
    metadata_fields = [
        "xyz_file",
        "sample_id",
        "adsorbate_expected",
        "adsorbate_actual",
        "adsorbate_valid",
        "adsorbate_symbols_last",
        "material",
        "material_elements",
        "n_atoms",
        "n_adsorbate_atoms",
        "n_catalyst_atoms",
        "error",
        "source_xyz_file",
        "source_round",
    ]

    for row in selected_rows:
        adsorbate = row["adsorbate"]
        source = resolve_project_path(row["xyz_file"])
        dest_dir = input_root / slugify(adsorbate) / "xyz"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{slugify(row.get('round', 'round'))}_{source.name}"
        shutil.copy2(source, dest)

        metadata = row.copy()
        metadata["source_xyz_file"] = row["xyz_file"]
        metadata["source_round"] = row.get("round", "")
        metadata["xyz_file"] = path_for_message(dest)
        metadata["sample_id"] = dest.stem
        by_adsorbate.setdefault(adsorbate, []).append(metadata)
        lookup[(adsorbate, dest.stem)] = metadata

    for adsorbate, rows in by_adsorbate.items():
        write_csv(
            input_root / slugify(adsorbate) / "xyz_metadata.csv",
            metadata_fields,
            rows,
        )

    return input_root, by_adsorbate, lookup


def copy_success_structure(row, success_dir):
    source = resolve_project_path(row["xyz_file"])
    adsorbate_dir = success_dir / slugify(row["adsorbate"])
    adsorbate_dir.mkdir(parents=True, exist_ok=True)
    destination = adsorbate_dir / source.name
    shutil.copy2(source, destination)
    return destination


def score_selected_rows(project_config, reaction, run_dir, selected_rows, adsorbates, log_file, dry_run):
    if dry_run or not selected_rows:
        return [], [], Counter()

    input_root, by_adsorbate, lookup = prepare_targeted_mlp_inputs(run_dir, selected_rows)
    score_root = run_dir / "targeted_mlp_scores"
    success_dir = run_dir / "success_structures"
    all_mlp_rows = []
    success_rows = []
    failure_counter = Counter()
    seen_fingerprints = set()

    for adsorbate in adsorbates:
        if adsorbate not in by_adsorbate:
            continue
        adsorbate_input = input_root / slugify(adsorbate)
        score_csv = score_root / f"{slugify(adsorbate)}_mlp_scores.csv"
        run_command(
            [
                sys.executable,
                "MLP_check/OC20_MLP/mlp_scores.py",
                "--config",
                project_config,
                "--adsorbate",
                adsorbate,
                "--xyz-dir",
                adsorbate_input / "xyz",
                "--metadata-csv",
                adsorbate_input / "xyz_metadata.csv",
                "--out-csv",
                score_csv,
                "--device",
                reaction.reaction_workflow.device.mlp,
            ],
            log_file,
        )

        for row in read_csv(score_csv):
            metadata = lookup.get((adsorbate, row.get("sample_id", "")), {})
            row.update(
                adsorbate=adsorbate,
                adsorbate_formula=metadata.get("adsorbate_formula", ""),
                round=metadata.get("round", metadata.get("source_round", "")),
                source_xyz_file=metadata.get("source_xyz_file", ""),
            )
            passed, reason = evaluate_mlp_row(row, reaction, seen_fingerprints)
            row["passed"] = str(passed).lower()
            row["failure_reason"] = reason
            if passed:
                copied = copy_success_structure(row, success_dir)
                row["success_xyz_file"] = path_for_message(copied)
                success_rows.append(row.copy())
            else:
                failure_counter[reason] += 1
            all_mlp_rows.append(row)

    return all_mlp_rows, success_rows, failure_counter


def write_reaction_outputs(run_dir, adsorbates, adsorbate_run_rows, pre_mlp_rows, pre_material_summary,
                           pre_matrix_rows, pre_dynamic_fields, selected_rows, all_mlp_rows,
                           success_rows, material_summary, matrix_rows, dynamic_fields, failure_counter,
                           log_file, report, report_path):
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

    pre_fields = [
        "adsorbate",
        "adsorbate_formula",
        "round",
        "xyz_file",
        "sample_id",
        "adsorbate_expected",
        "adsorbate_actual",
        "adsorbate_valid",
        "adsorbate_symbols_last",
        "material",
        "material_elements",
        "n_atoms",
        "n_adsorbate_atoms",
        "n_catalyst_atoms",
        "pre_mlp_passed",
        "pre_mlp_failure_reason",
        "error",
    ]
    write_csv(run_dir / "pre_mlp_candidates.csv", pre_fields, pre_mlp_rows)
    write_csv(
        run_dir / "pre_mlp_shared_material_summary.csv",
        [
            "material",
            "material_elements",
            "coverage_count",
            "required_adsorbate_count",
            "all_adsorbates_present",
            "missing_adsorbates",
            "total_pre_mlp_candidates",
        ],
        pre_material_summary,
    )
    write_csv(
        run_dir / "material_adsorbate_precheck_matrix.csv",
        [
            "material",
            "material_elements",
            "coverage_count",
            "required_adsorbate_count",
            "all_adsorbates_present",
            "missing_adsorbates",
            "total_pre_mlp_candidates",
        ]
        + pre_dynamic_fields,
        pre_matrix_rows,
    )
    write_csv(run_dir / "targeted_mlp_inputs.csv", pre_fields, selected_rows)

    mlp_fields = [
        "adsorbate",
        "adsorbate_formula",
        "round",
        "xyz_file",
        "source_xyz_file",
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
    write_csv(run_dir / "all_mlp_scored_candidates.csv", mlp_fields, all_mlp_rows)
    write_csv(run_dir / "all_success_structures.csv", mlp_fields, success_rows)
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
    write_csv(
        run_dir / "failure_summary.csv",
        ["failure_reason", "count"],
        [{"failure_reason": key, "count": value} for key, value in sorted(failure_counter.items())],
    )
    write_json(report_path, report)


def run_mlp_first(args, reaction, reaction_config, project_config, workflow_template, registry_entries,
                  registry_path, adsorbates, run_dir, log_file, child_config_dir, child_run_root):
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


def run_material_first(args, reaction, reaction_config, project_config, workflow_template, registry,
                       registry_entries, registry_path, adsorbates, run_dir, log_file, child_run_root):
    contexts = {
        adsorbate: adsorbate_context(registry, registry_entries, adsorbate)
        for adsorbate in adsorbates
    }
    target_shared_count = int(OmegaConf.select(reaction, "reaction_workflow.target_shared_material_count", default=1))
    min_pre_candidates = int(
        OmegaConf.select(
            reaction,
            "reaction_workflow.material_first.min_candidates_per_adsorbate_per_material",
            default=1,
        )
    )
    target_pre_materials = OmegaConf.select(
        reaction,
        "reaction_workflow.material_first.target_shared_material_candidates",
        default=None,
    )
    if target_pre_materials is None:
        target_pre_materials = target_shared_count
    target_pre_materials = int(target_pre_materials)

    max_rounds = int(reaction.reaction_workflow.per_adsorbate.max_rounds)
    rounds_to_run = 1 if args.dry_run else max_rounds
    pre_mlp_rows = []
    pre_failure_counter = Counter()
    round_reports = []
    adsorbate_counters = {
        adsorbate: {
            "metadata_candidates": 0,
            "pre_mlp_passed": 0,
            "parse_failed": 0,
        }
        for adsorbate in adsorbates
    }

    try:
        for round_idx in range(1, rounds_to_run + 1):
            round_name = f"round_{round_idx:04d}"
            round_report = {
                "round": round_name,
                "adsorbates": {},
            }

            for adsorbate in adsorbates:
                context = contexts[adsorbate]
                adsorbate_round_dir = child_run_root / slugify(adsorbate) / "rounds" / round_name
                outputs = run_metadata_round(
                    project_config,
                    reaction,
                    adsorbate,
                    adsorbate_round_dir,
                    context,
                    log_file,
                    args.dry_run,
                )

                parse_failed = 0 if args.dry_run else count_bad_samples(outputs["bad_samples"])
                if parse_failed:
                    pre_failure_counter["parse_failed"] += parse_failed
                    adsorbate_counters[adsorbate]["parse_failed"] += parse_failed

                rows = [] if args.dry_run else read_csv(outputs["metadata_csv"])
                round_valid = 0
                round_passed = 0
                for row in rows:
                    reason = metadata_failure_reason(row, reaction)
                    passed = reason == ""
                    if passed:
                        round_passed += 1
                    else:
                        pre_failure_counter[reason] += 1
                    if row.get("adsorbate_valid") == "true":
                        round_valid += 1

                    row.update(
                        adsorbate=adsorbate,
                        adsorbate_formula=context.get("formula", adsorbate),
                        round=round_name,
                        pre_mlp_passed=str(passed).lower(),
                        pre_mlp_failure_reason=reason,
                    )
                    pre_mlp_rows.append(row)

                adsorbate_counters[adsorbate]["metadata_candidates"] += len(rows)
                adsorbate_counters[adsorbate]["pre_mlp_passed"] += round_passed
                round_report["adsorbates"][adsorbate] = {
                    "round_dir": path_for_message(adsorbate_round_dir),
                    "metadata_candidates": len(rows),
                    "valid_adsorbate": round_valid,
                    "pre_mlp_passed": round_passed,
                    "parse_failed": parse_failed,
                }

            pre_material_summary, _, _ = aggregate_pre_mlp_materials(
                pre_mlp_rows,
                adsorbates,
                min_pre_candidates,
            )
            shared_candidate_count = sum(
                1 for row in pre_material_summary
                if row.get("all_adsorbates_present") == "true"
            )
            round_report["pre_mlp_shared_material_candidates"] = shared_candidate_count
            round_reports.append(round_report)

            if not args.dry_run and target_pre_materials > 0 and shared_candidate_count >= target_pre_materials:
                break

        pre_material_summary, pre_matrix_rows, pre_dynamic_fields = aggregate_pre_mlp_materials(
            pre_mlp_rows,
            adsorbates,
            min_pre_candidates,
        )
        pre_shared_count = sum(
            1 for row in pre_material_summary
            if row.get("all_adsorbates_present") == "true"
        )
        selected_materials = selected_materials_for_mlp(pre_material_summary, reaction)
        selected_rows = select_rows_for_targeted_mlp(pre_mlp_rows, selected_materials, reaction)

        all_mlp_rows, success_rows, mlp_failure_counter = score_selected_rows(
            project_config,
            reaction,
            run_dir,
            selected_rows,
            adsorbates,
            log_file,
            args.dry_run,
        )
        failure_counter = pre_failure_counter + mlp_failure_counter

        per_material_target = int(
            OmegaConf.select(
                reaction,
                "reaction_workflow.shared_material.target_success_per_adsorbate_per_material",
                default=1,
            )
        )
        material_summary, matrix_rows, dynamic_fields = aggregate_materials(
            success_rows,
            adsorbates,
            per_material_target,
        )
        shared_count = sum(1 for row in material_summary if row["all_adsorbates_passed"] == "true")

        adsorbate_run_rows = []
        for adsorbate in adsorbates:
            context = contexts[adsorbate]
            counters = adsorbate_counters[adsorbate]
            adsorbate_success = sum(1 for row in success_rows if row.get("adsorbate") == adsorbate)
            adsorbate_run_rows.append(
                {
                    "adsorbate": adsorbate,
                    "formula": context.get("formula", adsorbate),
                    "status": "dry_run" if args.dry_run else "metadata_first_complete",
                    "success": adsorbate_success,
                    "target_success_count": "",
                    "candidates": counters["metadata_candidates"],
                    "run_dir": path_for_message(child_run_root / slugify(adsorbate)),
                    "workflow_report": "",
                    "success_summary_csv": "",
                    "failure_summary_csv": "",
                    "viewer_index": "",
                    "checkpoint_path": path_for_message(context["checkpoint_path"]),
                }
            )

        status = "dry_run" if args.dry_run else ("success" if shared_count >= target_shared_count else "incomplete")
        report = {
            "status": status,
            "screening_mode": "material_first",
            "run_dir": path_for_message(run_dir),
            "reaction_config": path_for_message(reaction_config),
            "workflow_template": path_for_message(workflow_template),
            "project_config": path_for_message(project_config),
            "adsorbate_registry": path_for_message(registry_path),
            "adsorbates": adsorbates,
            "rounds": round_reports,
            "filters": {
                "require_adsorbate_valid": bool(
                    OmegaConf.select(reaction, "reaction_workflow.filters.require_adsorbate_valid", default=True)
                ),
                "mlp_energy_gt": OmegaConf.select(reaction, "reaction_workflow.filters.mlp_energy_gt", default=None),
                "mlp_energy_lt": OmegaConf.select(reaction, "reaction_workflow.filters.mlp_energy_lt", default=None),
            },
            "material_query": OmegaConf.to_container(reaction.reaction_workflow.material_query, resolve=True),
            "material_first": OmegaConf.to_container(
                OmegaConf.select(reaction, "reaction_workflow.material_first", default={}),
                resolve=True,
            ),
            "selected_materials_for_mlp": selected_materials,
            "counts": {
                "adsorbates": len(adsorbates),
                "pre_mlp_candidates": len(pre_mlp_rows),
                "pre_mlp_passed_candidates": sum(1 for row in pre_mlp_rows if row.get("pre_mlp_passed") == "true"),
                "pre_mlp_shared_materials": pre_shared_count,
                "selected_materials_for_mlp": len(selected_materials),
                "targeted_mlp_inputs": len(selected_rows),
                "mlp_scored_candidates": len(all_mlp_rows),
                "success_structures": len(success_rows),
                "shared_materials": shared_count,
                "target_shared_material_count": target_shared_count,
                "failures": dict(failure_counter),
            },
            "outputs": {
                "adsorbate_run_summary_csv": path_for_message(run_dir / "adsorbate_run_summary.csv"),
                "pre_mlp_candidates_csv": path_for_message(run_dir / "pre_mlp_candidates.csv"),
                "pre_mlp_shared_material_summary_csv": path_for_message(run_dir / "pre_mlp_shared_material_summary.csv"),
                "material_adsorbate_precheck_matrix_csv": path_for_message(run_dir / "material_adsorbate_precheck_matrix.csv"),
                "targeted_mlp_inputs_csv": path_for_message(run_dir / "targeted_mlp_inputs.csv"),
                "all_mlp_scored_candidates_csv": path_for_message(run_dir / "all_mlp_scored_candidates.csv"),
                "all_success_structures_csv": path_for_message(run_dir / "all_success_structures.csv"),
                "shared_material_summary_csv": path_for_message(run_dir / "shared_material_summary.csv"),
                "material_adsorbate_matrix_csv": path_for_message(run_dir / "material_adsorbate_matrix.csv"),
                "failure_summary_csv": path_for_message(run_dir / "failure_summary.csv"),
                "reaction_run_log": path_for_message(log_file),
            },
        }
        report_path = run_dir / "reaction_workflow_report.json"
        write_reaction_outputs(
            run_dir,
            adsorbates,
            adsorbate_run_rows,
            pre_mlp_rows,
            pre_material_summary,
            pre_matrix_rows,
            pre_dynamic_fields,
            selected_rows,
            all_mlp_rows,
            success_rows,
            material_summary,
            matrix_rows,
            dynamic_fields,
            failure_counter,
            log_file,
            report,
            report_path,
        )
    except Exception as exc:
        report = {
            "status": "failed",
            "screening_mode": "material_first",
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

    screening_mode = OmegaConf.select(reaction, "reaction_workflow.screening_mode", default="mlp_first")
    if screening_mode == "mlp_first":
        run_mlp_first(
            args,
            reaction,
            reaction_config,
            project_config,
            workflow_template,
            registry_entries,
            registry_path,
            adsorbates,
            run_dir,
            log_file,
            child_config_dir,
            child_run_root,
        )
    elif screening_mode == "material_first":
        run_material_first(
            args,
            reaction,
            reaction_config,
            project_config,
            workflow_template,
            registry,
            registry_entries,
            registry_path,
            adsorbates,
            run_dir,
            log_file,
            child_run_root,
        )
    else:
        raise ValueError(f"Unsupported reaction_workflow.screening_mode: {screening_mode}")


if __name__ == "__main__":
    main()
