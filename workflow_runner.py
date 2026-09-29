#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import webbrowser
from collections import Counter
from datetime import datetime
from pathlib import Path

from omegaconf import OmegaConf


PROJECT_ROOT = Path(__file__).resolve().parent


def parse_args():
    parser = argparse.ArgumentParser(description="Run the local structure-generation workflow.")
    parser.add_argument("--workflow-config", default="config/workflow_config.yml")
    parser.add_argument("--project-config", default="config/config.yml")
    parser.add_argument("--adsorbate", help="Override workflow.adsorbate.")
    parser.add_argument("--run-dir", help="Override workflow output run directory.")
    parser.add_argument("--dry-run", action="store_true", help="Print and log commands without executing them.")
    parser.add_argument("--open-viewer", action="store_true", help="Open viewer/index.html after the workflow finishes.")
    parser.add_argument(
        "--overwrite-run-dir",
        action="store_true",
        help="Delete an existing run directory before writing new outputs.",
    )
    parser.add_argument(
        "--allow-existing-run-dir",
        action="store_true",
        help="Allow writing into an existing non-empty run directory. Use only for debugging.",
    )
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


def format_template(template, adsorbate):
    return str(template).format(adsorbate=adsorbate)


def checkpoint_sort_key(path):
    name = path.name
    if name.startswith("checkpoint-"):
        suffix = name.split("checkpoint-", 1)[1]
        if suffix.isdigit():
            return int(suffix)
    return -1


def find_latest_checkpoint(project_params, adsorbate):
    output_dir = resolve_project_path(format_template(project_params.paths.output_dir_template, adsorbate))
    if not output_dir.is_dir():
        return None

    checkpoints = [
        path for path in output_dir.iterdir()
        if path.is_dir() and path.name.startswith("checkpoint")
    ]
    if not checkpoints:
        return None
    return max(checkpoints, key=lambda path: (checkpoint_sort_key(path), path.stat().st_mtime))


def has_tokenizer(path):
    path = Path(path)
    return (path / "tokenizer.json").is_file() or (path / "vocab.txt").is_file()


def load_adsorbate_registry(workflow):
    registry_path = OmegaConf.select(workflow, "workflow.adsorbate_registry", default="config/adsorbates.yml")
    if not registry_path:
        return None, None

    registry_path = resolve_project_path(registry_path)
    if not registry_path.is_file():
        return None, registry_path

    return OmegaConf.load(registry_path), registry_path


def find_registry_entry(registry, adsorbate):
    if registry is None:
        return None

    for entry in registry.adsorbates:
        if entry.name == adsorbate:
            return entry
    return None


def registry_checkpoint_context(registry, adsorbate):
    entry = find_registry_entry(registry, adsorbate)
    if entry is None:
        return None
    if not OmegaConf.select(entry, "enabled", default=True):
        raise ValueError(f"Adsorbate is disabled in registry: {adsorbate}")

    template = registry.checkpoint_dir_template
    checkpoint_path = resolve_project_path(format_template(template, adsorbate))
    required_files = OmegaConf.select(registry, "required_files", default=[]) or []
    missing = [name for name in required_files if not (checkpoint_path / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"Checkpoint for {adsorbate} is missing required files in {path_for_message(checkpoint_path)}: "
            + ", ".join(missing)
        )

    return {
        "adsorbate": adsorbate,
        "formula": OmegaConf.select(entry, "formula", default=adsorbate),
        "checkpoint_path": checkpoint_path,
        "tokenizer_path": checkpoint_path if has_tokenizer(checkpoint_path) else None,
        "source": "adsorbate_registry",
    }


def fallback_checkpoint_context(project_params, adsorbate):
    checkpoint_path = find_latest_checkpoint(project_params, adsorbate)
    return {
        "adsorbate": adsorbate,
        "formula": adsorbate,
        "checkpoint_path": checkpoint_path,
        "tokenizer_path": checkpoint_path if checkpoint_path is not None and has_tokenizer(checkpoint_path) else None,
        "source": "outputs_finetune",
    }


def load_adsorbate_context(project_params, workflow, adsorbate):
    registry, _ = load_adsorbate_registry(workflow)
    context = registry_checkpoint_context(registry, adsorbate)
    if context is not None:
        return context
    return fallback_checkpoint_context(project_params, adsorbate)


def slugify(value):
    keep = []
    for char in str(value):
        if char.isalnum() or char in ["-", "_"]:
            keep.append(char)
        else:
            keep.append("_")
    return "".join(keep).strip("_") or "workflow"


def make_run_dir(workflow, args):
    if args.run_dir:
        return resolve_project_path(args.run_dir)

    run_name = workflow.workflow.run_name
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if run_name is None:
        material = OmegaConf.select(workflow, "workflow.material_query.material", default="material")
        energy = OmegaConf.select(workflow, "workflow.filters.mlp_energy_lt", default="none")
        run_name = f"{workflow.workflow.adsorbate}_{material}_E_lt_{energy}_{timestamp}"
    elif args.dry_run:
        run_name = f"{run_name}_dryrun_{timestamp}"

    output_root = resolve_project_path(workflow.workflow.output_root)
    return output_root / slugify(run_name)


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


def build_structure_previews(workflow, success_summary, run_dir, adsorbate, log_file, dry_run=False):
    preview_dir = run_dir / "structure_previews"
    enabled = bool(OmegaConf.select(workflow, "workflow.preview.enabled", default=True))
    outputs = {
        "structure_previews": path_for_message(preview_dir),
        "preview_summary_csv": path_for_message(preview_dir / "preview_summary.csv"),
        "preview_manifest_json": path_for_message(preview_dir / "preview_manifest.json"),
    }
    if not enabled:
        return outputs

    command = [
        sys.executable,
        "script/render_xyz_preview.py",
        "--success-summary",
        success_summary,
        "--output-dir",
        preview_dir,
        "--adsorbate",
        adsorbate,
        "--max-structures",
        int(OmegaConf.select(workflow, "workflow.preview.max_structures", default=5)),
        "--frames",
        int(OmegaConf.select(workflow, "workflow.preview.frames", default=12)),
        "--image-width",
        int(OmegaConf.select(workflow, "workflow.preview.image_width", default=720)),
        "--image-height",
        int(OmegaConf.select(workflow, "workflow.preview.image_height", default=540)),
        "--gif-duration-ms",
        int(OmegaConf.select(workflow, "workflow.preview.gif_duration_ms", default=160)),
    ]
    if not bool(OmegaConf.select(workflow, "workflow.preview.gif", default=True)):
        command.append("--no-gif")
    run_command(command, log_file, dry_run=dry_run)
    return outputs


def is_relative_to(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def prepare_run_dir(run_dir, args):
    run_dir = Path(run_dir).resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        if args.overwrite_run_dir:
            output_root = resolve_project_path("outputs")
            if run_dir == PROJECT_ROOT or not is_relative_to(run_dir, output_root):
                raise ValueError(f"Refusing to overwrite run directory outside outputs/: {path_for_message(run_dir)}")
            shutil.rmtree(run_dir)
        elif not args.allow_existing_run_dir:
            raise FileExistsError(
                f"Run directory already exists and is not empty: {path_for_message(run_dir)}. "
                "Use a unique workflow.run_name, pass --overwrite-run-dir, or pass --allow-existing-run-dir."
            )
    run_dir.mkdir(parents=True, exist_ok=True)


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


def count_bad_samples(path):
    if not path.is_file():
        return 0
    with open(path, "r", encoding="utf-8") as fr:
        return sum(1 for line in fr if line.strip())


def file_fingerprint(path):
    return Path(path).read_bytes()


def evaluate_candidate(row, workflow, seen_fingerprints):
    require_adsorbate = OmegaConf.select(workflow, "workflow.filters.require_adsorbate_valid", default=True)
    energy_gt = OmegaConf.select(workflow, "workflow.filters.mlp_energy_gt", default=None)
    energy_lt = OmegaConf.select(workflow, "workflow.filters.mlp_energy_lt", default=None)
    duplicate_check = OmegaConf.select(workflow, "workflow.duplicate_check.enabled", default=False)

    if row.get("error"):
        return False, "mlp_error"
    if require_adsorbate and row.get("adsorbate_valid") != "true":
        return False, "wrong_adsorbate"
    if not material_matches(row, workflow.workflow.material_query):
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


def copy_structure(row, destination_dir, round_name):
    source = resolve_project_path(row["xyz_file"])
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{round_name}_{source.name}"
    shutil.copy2(source, destination)
    return destination


def build_generate_command(project_config, adsorbate, round_dir, workflow, adsorbate_context):
    generation = workflow.workflow.generation
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
        workflow.workflow.generation_per_round,
        "--device",
        workflow.workflow.device.generation,
    ]
    if adsorbate_context.get("checkpoint_path") is not None:
        command.extend(["--ckpt-path", adsorbate_context["checkpoint_path"]])
    if adsorbate_context.get("tokenizer_path") is not None:
        command.extend(["--tokenizer-path", adsorbate_context["tokenizer_path"]])
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


def run_round(project_config, adsorbate, round_dir, workflow, log_file, dry_run, adsorbate_context):
    round_dir.mkdir(parents=True, exist_ok=True)
    generate_command, pkl_path = build_generate_command(project_config, adsorbate, round_dir, workflow, adsorbate_context)
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
    if adsorbate_context.get("tokenizer_path") is not None:
        decode_command.extend(["--tokenizer-path", adsorbate_context["tokenizer_path"]])
    run_command(decode_command, log_file, dry_run=dry_run)

    index_command = [
        sys.executable,
        "script/index_xyz_metadata.py",
        "--config",
        project_config,
        "--adsorbate",
        adsorbate,
        "--adsorbate-formula",
        adsorbate_context.get("formula", adsorbate),
        "--xyz-dir",
        analysis_dir / "xyz",
        "--output-dir",
        analysis_dir,
    ]
    run_command(index_command, log_file, dry_run=dry_run)
    run_command(
        [
            sys.executable,
            "MLP_check/OC20_MLP/mlp_scores.py",
            "--config",
            project_config,
            "--adsorbate",
            adsorbate,
            "--xyz-dir",
            analysis_dir / "xyz",
            "--metadata-csv",
            analysis_dir / "xyz_metadata.csv",
            "--out-csv",
            analysis_dir / "mlp_scores.csv",
            "--device",
            workflow.workflow.device.mlp,
        ],
        log_file,
        dry_run=dry_run,
    )

    return {
        "round_dir": round_dir,
        "analysis_dir": analysis_dir,
        "mlp_scores": analysis_dir / "mlp_scores.csv",
        "bad_samples": analysis_dir / "bad_samples.txt",
    }


def write_report(run_dir, report):
    report_path = run_dir / "workflow_report.json"
    with open(report_path, "w", encoding="utf-8") as fw:
        json.dump(report, fw, indent=2)
    return report_path


def should_open_viewer(workflow, args):
    return args.open_viewer or OmegaConf.select(workflow, "workflow.viewer.auto_open", default=False)


def open_viewer_file(viewer_index):
    if hasattr(os, "startfile"):
        os.startfile(str(viewer_index))
        return
    webbrowser.open(viewer_index.as_uri())


def main():
    args = parse_args()
    project_config = resolve_project_path(args.project_config)
    workflow_config = resolve_project_path(args.workflow_config)
    project_params = OmegaConf.load(project_config)
    workflow = OmegaConf.load(workflow_config)

    if args.adsorbate:
        workflow.workflow.adsorbate = args.adsorbate
    adsorbate = workflow.workflow.adsorbate
    adsorbate_context = load_adsorbate_context(project_params, workflow, adsorbate)

    run_dir = make_run_dir(workflow, args)
    prepare_run_dir(run_dir, args)
    log_file = run_dir / "run.log"
    success_dir = run_dir / "success_structures"
    failed_dir = run_dir / "failed_structures"

    report = {
        "status": "running",
        "dry_run": bool(args.dry_run),
        "adsorbate": adsorbate,
        "run_dir": path_for_message(run_dir),
        "workflow_config": path_for_message(workflow_config),
        "project_config": path_for_message(project_config),
        "target_success_count": int(workflow.workflow.target_success_count),
        "adsorbate_formula": adsorbate_context.get("formula", adsorbate),
        "checkpoint_source": adsorbate_context.get("source"),
        "checkpoint_path": path_for_message(adsorbate_context["checkpoint_path"]) if adsorbate_context.get("checkpoint_path") else None,
        "filters": {
            "require_adsorbate_valid": bool(OmegaConf.select(workflow, "workflow.filters.require_adsorbate_valid", default=True)),
            "mlp_energy_gt": OmegaConf.select(workflow, "workflow.filters.mlp_energy_gt", default=None),
            "mlp_energy_lt": OmegaConf.select(workflow, "workflow.filters.mlp_energy_lt", default=None),
        },
        "material_query": OmegaConf.to_container(workflow.workflow.material_query, resolve=True),
        "rounds": [],
        "counts": {},
        "outputs": {},
    }

    all_candidates = []
    success_rows = []
    failure_counter = Counter()
    seen_fingerprints = set()

    try:
        fine_tune_enabled = OmegaConf.select(workflow, "workflow.fine_tune.enabled", default=False)
        require_checkpoint = OmegaConf.select(workflow, "workflow.fine_tune.require_existing_checkpoint", default=True)
        if fine_tune_enabled:
            run_command(
                [sys.executable, "train_local.py", "--config", project_config, "--adsorbate", adsorbate],
                log_file,
                dry_run=args.dry_run,
            )
        elif require_checkpoint and adsorbate_context.get("checkpoint_path") is None:
            raise FileNotFoundError(f"No checkpoint-* found for adsorbate {adsorbate}. Run fine-tuning first.")

        for round_idx in range(1, int(workflow.workflow.max_rounds) + 1):
            if len(success_rows) >= int(workflow.workflow.target_success_count):
                break

            round_name = f"round_{round_idx:04d}"
            round_dir = run_dir / "rounds" / round_name
            round_outputs = run_round(project_config, adsorbate, round_dir, workflow, log_file, args.dry_run, adsorbate_context)

            rows = [] if args.dry_run else read_csv(round_outputs["mlp_scores"])
            parse_failed = 0 if args.dry_run else count_bad_samples(round_outputs["bad_samples"])
            if parse_failed:
                failure_counter["parse_failed"] += parse_failed

            round_success = 0
            for row in rows:
                passed, reason = evaluate_candidate(row, workflow, seen_fingerprints)
                row.update(
                    round=round_name,
                    passed=str(passed).lower(),
                    failure_reason=reason,
                )
                all_candidates.append(row)

                if passed and len(success_rows) < int(workflow.workflow.target_success_count):
                    copied = copy_structure(row, success_dir, round_name)
                    row["success_xyz_file"] = path_for_message(copied)
                    success_rows.append(row.copy())
                    round_success += 1
                elif not passed:
                    failure_counter[reason] += 1
                    if OmegaConf.select(workflow, "workflow.keep_failed_structures", default=False) and row.get("xyz_file"):
                        reason_dir = failed_dir / reason
                        copy_structure(row, reason_dir, round_name)

            report["rounds"].append(
                {
                    "round": round_name,
                    "round_dir": path_for_message(round_dir),
                    "processed_by_mlp": len(rows),
                    "parse_failed": parse_failed,
                    "new_success": round_success,
                    "total_success": len(success_rows),
                }
            )

        candidate_fields = [
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
        ]
        success_fields = candidate_fields + ["success_xyz_file"]
        write_csv(run_dir / "all_candidates.csv", candidate_fields, all_candidates)
        write_csv(run_dir / "success_summary.csv", success_fields, success_rows)
        write_csv(
            run_dir / "failure_summary.csv",
            ["failure_reason", "count"],
            [{"failure_reason": key, "count": value} for key, value in sorted(failure_counter.items())],
        )
        viewer_dir = run_dir / "viewer"
        run_command(
            [
                sys.executable,
                "script/build_structure_gallery.py",
                "--adsorbate",
                adsorbate_context.get("formula", adsorbate),
                "--success-summary",
                run_dir / "success_summary.csv",
                "--output-dir",
                viewer_dir,
                "--title",
                f"{adsorbate} accepted structures",
            ],
            log_file,
            dry_run=args.dry_run,
        )
        preview_outputs = build_structure_previews(
            workflow,
            run_dir / "success_summary.csv",
            run_dir,
            adsorbate,
            log_file,
            dry_run=args.dry_run,
        )

        report["status"] = "success" if len(success_rows) >= int(workflow.workflow.target_success_count) else "incomplete"
        report["counts"] = {
            "success": len(success_rows),
            "target_success_count": int(workflow.workflow.target_success_count),
            "candidates": len(all_candidates),
            "failures": dict(failure_counter),
        }
        viewer_index = viewer_dir / "index.html"
        report["outputs"] = {
            "success_structures": path_for_message(success_dir),
            "all_candidates_csv": path_for_message(run_dir / "all_candidates.csv"),
            "success_summary_csv": path_for_message(run_dir / "success_summary.csv"),
            "failure_summary_csv": path_for_message(run_dir / "failure_summary.csv"),
            "viewer_index": path_for_message(viewer_index),
            "run_log": path_for_message(log_file),
            **preview_outputs,
        }
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        write_report(run_dir, report)
        raise

    report_path = write_report(run_dir, report)
    viewer_index = run_dir / "viewer" / "index.html"
    if not args.dry_run and should_open_viewer(workflow, args) and viewer_index.is_file():
        open_viewer_file(viewer_index)
    print(f"[done] status={report['status']}")
    print(f"[done] success={report['counts']['success']}/{report['counts']['target_success_count']}")
    print(f"[done] report={path_for_message(report_path)}")


if __name__ == "__main__":
    main()
