#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import os
import re
from pathlib import Path

import numpy as np
from ase.io import read
from fairchem.core.common.relaxation.ase_utils import OCPCalculator
from omegaconf import OmegaConf
from tqdm import tqdm


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_DIR = Path(__file__).resolve().parent / "MLP_model"


def parse_args():
    p = argparse.ArgumentParser(description="Score generated xyz structures with an OC20 MLP calculator.")
    p.add_argument("--config", default="config/config.yml", help="Path to the training config YAML file.")
    p.add_argument("--adsorbate", help="Adsorbate name. Overrides experiment.adsorbate in the config.")
    p.add_argument("--xyz-dir", help="Directory containing xyz files to score.")
    p.add_argument("--out-csv", help="Output CSV path.")
    p.add_argument("--metadata-csv", help="Optional xyz_metadata.csv to join material/adsorbate metadata.")
    p.add_argument("--model-name", help="fairchem pretrained model name.")
    p.add_argument("--model-cache", help="Local directory containing downloaded MLP checkpoints.")
    p.add_argument("--checkpoint-path", help="Direct local checkpoint path, if supported by the installed fairchem version.")
    p.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"], help="auto, cpu, or cuda.")
    p.add_argument("--max-files", type=int, help="Maximum number of xyz files to score.")
    p.add_argument("--pbc-z", action="store_true", help="Keep periodicity along z. Default is slab pbc=(T,T,F).")
    return p.parse_args()


def resolve_project_path(path):
    path = Path(path)
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


def resolve_path(path, base=PROJECT_ROOT):
    path = Path(path)
    if path.is_absolute():
        return path
    return (base / path).resolve()


def format_template(template, adsorbate):
    return str(template).format(adsorbate=adsorbate)


def path_for_message(path):
    path = Path(path)
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def natural_sort_key(path):
    parts = re.split(r"(\d+)", path.name)
    return [int(part) if part.isdigit() else part for part in parts]


def load_paths(config_path, args):
    params = OmegaConf.load(config_path)
    adsorbate = args.adsorbate or params.experiment.adsorbate
    params.experiment.adsorbate = adsorbate

    output_dir = resolve_project_path(format_template(params.paths.output_dir_template, adsorbate))
    analysis_dir_name = OmegaConf.select(params, "paths.analysis_dir_name", default="analysis")
    analysis_dir = output_dir / analysis_dir_name / f"generated_{adsorbate}"

    xyz_dir = resolve_project_path(args.xyz_dir) if args.xyz_dir else analysis_dir / "xyz"
    out_csv = resolve_project_path(args.out_csv) if args.out_csv else analysis_dir / "mlp_scores.csv"
    metadata_csv = (
        resolve_project_path(args.metadata_csv)
        if args.metadata_csv
        else analysis_dir / "xyz_metadata.csv"
    )
    return params, xyz_dir, out_csv, metadata_csv


def read_metadata(metadata_csv):
    if not metadata_csv.is_file():
        return {}

    rows = {}
    with open(metadata_csv, "r", newline="", encoding="utf-8") as fr:
        reader = csv.DictReader(fr)
        for row in reader:
            rows[row.get("sample_id", "")] = row
    return rows


def select_cpu(device_arg):
    if device_arg == "cpu":
        return True
    if device_arg == "cuda":
        return False
    try:
        import torch

        return not torch.cuda.is_available()
    except ImportError:
        return True


def build_calculator(args, params):
    model_name = args.model_name or OmegaConf.select(
        params,
        "mlp.model_name",
        default="EquiformerV2-31M-S2EF-OC20-All+MD",
    )
    model_cache = resolve_path(
        args.model_cache or OmegaConf.select(params, "mlp.model_cache", default=str(DEFAULT_MODEL_DIR)),
        base=PROJECT_ROOT,
    )
    cpu = select_cpu(args.device)

    if args.checkpoint_path:
        checkpoint_path = resolve_project_path(args.checkpoint_path)
        if not checkpoint_path.is_file():
            raise FileNotFoundError(f"Missing MLP checkpoint: {path_for_message(checkpoint_path)}")
        return OCPCalculator(checkpoint_path=str(checkpoint_path), cpu=cpu)

    if not model_cache.is_dir():
        raise FileNotFoundError(f"Missing MLP model cache directory: {path_for_message(model_cache)}")

    return OCPCalculator(
        model_name=model_name,
        local_cache=str(model_cache),
        cpu=cpu,
    )


def score_atoms(calc, xyz_path, pbc_z):
    atoms = read(str(xyz_path))
    atoms.pbc = (True, True, bool(pbc_z))
    atoms.calc = calc

    energy = float(atoms.get_potential_energy())
    forces = atoms.get_forces()
    force_rms = float(np.sqrt((forces**2).mean()))
    force_max = float(np.linalg.norm(forces, axis=1).max())
    return atoms, energy, force_rms, force_max


def write_csv(path, fieldnames, rows):
    os.makedirs(path.parent, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fw:
        writer = csv.DictWriter(fw, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_ranked_rows(rows):
    scored_rows = [row.copy() for row in rows if row["E_pred"] != "" and row["error"] == ""]
    scored_rows.sort(key=lambda row: float(row["E_pred"]))
    for rank, row in enumerate(scored_rows, start=1):
        row["rank_by_E"] = rank
    return scored_rows


def main():
    args = parse_args()
    config_path = resolve_project_path(args.config)
    params, xyz_dir, out_csv, metadata_csv = load_paths(config_path, args)

    if not xyz_dir.is_dir():
        raise FileNotFoundError(f"Missing xyz directory: {path_for_message(xyz_dir)}")

    xyz_files = sorted(xyz_dir.glob("*.xyz"), key=natural_sort_key)
    if args.max_files is not None:
        xyz_files = xyz_files[:args.max_files]

    print(f"[config] adsorbate={params.experiment.adsorbate}")
    print(f"[paths] xyz_dir={path_for_message(xyz_dir)}")
    print(f"[paths] out_csv={path_for_message(out_csv)}")
    if metadata_csv.is_file():
        print(f"[paths] metadata_csv={path_for_message(metadata_csv)}")

    calc = build_calculator(args, params)
    metadata = read_metadata(metadata_csv)

    fieldnames = [
        "xyz_file",
        "sample_id",
        "natoms",
        "E_pred",
        "F_rms",
        "F_max",
        "material",
        "material_elements",
        "adsorbate_valid",
        "error",
    ]
    rows = []

    for xyz_path in tqdm(xyz_files, desc="mlp scoring"):
        sample_id = xyz_path.stem
        row_meta = metadata.get(sample_id, {})
        row = {
            "xyz_file": path_for_message(xyz_path),
            "sample_id": sample_id,
            "natoms": "",
            "E_pred": "",
            "F_rms": "",
            "F_max": "",
            "material": row_meta.get("material", ""),
            "material_elements": row_meta.get("material_elements", ""),
            "adsorbate_valid": row_meta.get("adsorbate_valid", ""),
            "error": "",
        }

        try:
            atoms, energy, force_rms, force_max = score_atoms(calc, xyz_path, args.pbc_z)
            row.update(
                natoms=len(atoms),
                E_pred=energy,
                F_rms=force_rms,
                F_max=force_max,
            )
        except Exception as exc:
            row["error"] = str(exc)

        rows.append(row)

    write_csv(out_csv, fieldnames, rows)

    ranked_csv = out_csv.with_name(out_csv.stem + "_ranked.csv")
    write_csv(ranked_csv, ["rank_by_E"] + fieldnames, build_ranked_rows(rows))

    print(f"[done] wrote: {path_for_message(out_csv)}")
    print(f"[done] ranked: {path_for_message(ranked_csv)}")


if __name__ == "__main__":
    main()
