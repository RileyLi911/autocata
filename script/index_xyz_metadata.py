#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import os
import re
from collections import Counter
from pathlib import Path

from omegaconf import OmegaConf
from tqdm import tqdm


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args():
    p = argparse.ArgumentParser(description="Build a searchable material/adsorbate index from generated xyz files.")
    p.add_argument("--config", default="config/config.yml", help="Path to the training config YAML file.")
    p.add_argument("--adsorbate", help="Adsorbate formula. Overrides experiment.adsorbate in the config.")
    p.add_argument("--adsorbate-formula", help="Formula to validate at the end of each xyz. Defaults to adsorbate.")
    p.add_argument("--xyz-dir", help="Input xyz directory.")
    p.add_argument("--output-dir", help="Directory for index CSV files.")
    p.add_argument("--max-files", type=int, help="Maximum number of xyz files to index.")
    return p.parse_args()


def resolve_project_path(path):
    path = Path(path)
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


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


def parse_formula(formula):
    tokens = re.findall(r"([A-Z][a-z]?)(\d*)", formula)
    if not tokens or "".join(element + count for element, count in tokens) != formula:
        raise ValueError(f"Unsupported adsorbate formula: {formula}")

    counts = Counter()
    for element, count in tokens:
        counts[element] += int(count) if count else 1
    return counts


def formula_to_string(counts):
    pieces = []
    for element in sorted(counts):
        count = counts[element]
        pieces.append(element if count == 1 else f"{element}{count}")
    return "".join(pieces)


def read_xyz_symbols(path):
    with open(path, "r", encoding="utf-8") as fr:
        first = fr.readline().strip()
        try:
            n_atoms = int(first)
        except ValueError as exc:
            raise ValueError(f"Invalid xyz atom count in {path_for_message(path)}") from exc

        fr.readline()
        symbols = []
        for _ in range(n_atoms):
            line = fr.readline()
            if not line:
                break
            parts = line.split()
            if parts:
                symbols.append(parts[0])

    if len(symbols) != n_atoms:
        raise ValueError(f"Expected {n_atoms} atoms but read {len(symbols)} in {path_for_message(path)}")
    return symbols


def material_name(symbols):
    elements = sorted(set(symbols))
    return "-".join(elements), ";".join(elements)


def load_paths(config_path, args):
    params = OmegaConf.load(config_path)
    adsorbate = args.adsorbate or params.experiment.adsorbate
    params.experiment.adsorbate = adsorbate

    output_dir = resolve_project_path(format_template(params.paths.output_dir_template, adsorbate))
    analysis_dir_name = OmegaConf.select(params, "paths.analysis_dir_name", default="analysis")
    analysis_dir = output_dir / analysis_dir_name / f"generated_{adsorbate}"

    xyz_dir = resolve_project_path(args.xyz_dir) if args.xyz_dir else analysis_dir / "xyz"
    index_dir = resolve_project_path(args.output_dir) if args.output_dir else analysis_dir
    return params, xyz_dir, index_dir


def write_rows(path, fieldnames, rows):
    os.makedirs(path.parent, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fw:
        writer = csv.DictWriter(fw, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_material_summary(rows):
    grouped = {}
    for row in rows:
        material = row["material"]
        if material not in grouped:
            grouped[material] = {
                "material": material,
                "material_elements": row["material_elements"],
                "total_structures": 0,
                "valid_adsorbate_structures": 0,
            }
        grouped[material]["total_structures"] += 1
        if row["adsorbate_valid"] == "true":
            grouped[material]["valid_adsorbate_structures"] += 1

    return sorted(grouped.values(), key=lambda item: item["material"])


def main():
    args = parse_args()
    config_path = resolve_project_path(args.config)
    params, xyz_dir, output_dir = load_paths(config_path, args)

    adsorbate_formula = args.adsorbate_formula or params.experiment.adsorbate
    expected_counts = parse_formula(adsorbate_formula)
    expected_n = sum(expected_counts.values())

    if not xyz_dir.is_dir():
        raise FileNotFoundError(f"Missing xyz directory: {path_for_message(xyz_dir)}")

    xyz_files = sorted(xyz_dir.glob("*.xyz"), key=natural_sort_key)
    if args.max_files is not None:
        xyz_files = xyz_files[:args.max_files]

    print(f"[config] adsorbate={params.experiment.adsorbate}")
    print(f"[check] adsorbate_formula={adsorbate_formula} atoms={expected_n}")
    print(f"[paths] xyz_dir={path_for_message(xyz_dir)}")
    print(f"[paths] output_dir={path_for_message(output_dir)}")

    rows = []
    invalid_rows = []
    for xyz_path in tqdm(xyz_files, desc="indexing xyz"):
        error = ""
        symbols = []
        try:
            symbols = read_xyz_symbols(xyz_path)
            ads_symbols = symbols[-expected_n:] if len(symbols) >= expected_n else symbols
            catalyst_symbols = symbols[:-expected_n] if len(symbols) >= expected_n else []
            actual_counts = Counter(ads_symbols)
            adsorbate_valid = actual_counts == expected_counts
            material, material_elements = material_name(catalyst_symbols)
        except Exception as exc:
            ads_symbols = []
            catalyst_symbols = []
            actual_counts = Counter()
            adsorbate_valid = False
            material = ""
            material_elements = ""
            error = str(exc)

        row = {
            "xyz_file": path_for_message(xyz_path),
            "sample_id": xyz_path.stem,
            "adsorbate_expected": formula_to_string(expected_counts),
            "adsorbate_actual": formula_to_string(actual_counts) if actual_counts else "",
            "adsorbate_valid": str(adsorbate_valid).lower(),
            "adsorbate_symbols_last": ";".join(ads_symbols),
            "material": material,
            "material_elements": material_elements,
            "n_atoms": len(symbols),
            "n_adsorbate_atoms": len(ads_symbols),
            "n_catalyst_atoms": len(catalyst_symbols),
            "error": error,
        }
        rows.append(row)
        if not adsorbate_valid:
            invalid_rows.append(row)

    fieldnames = [
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
    ]
    index_path = output_dir / "xyz_metadata.csv"
    invalid_path = output_dir / "xyz_invalid_adsorbate.csv"
    summary_path = output_dir / "material_summary.csv"

    write_rows(index_path, fieldnames, rows)
    write_rows(invalid_path, fieldnames, invalid_rows)
    write_rows(
        summary_path,
        ["material", "material_elements", "total_structures", "valid_adsorbate_structures"],
        build_material_summary(rows),
    )

    valid_count = len(rows) - len(invalid_rows)
    print(f"[done] indexed={len(rows)} valid_adsorbate={valid_count} invalid_adsorbate={len(invalid_rows)}")
    print(f"[done] metadata={path_for_message(index_path)}")
    print(f"[done] invalid={path_for_message(invalid_path)}")
    print(f"[done] material_summary={path_for_message(summary_path)}")


if __name__ == "__main__":
    main()
