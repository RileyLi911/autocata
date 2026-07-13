#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import os
import pickle
import re
import sys
from pathlib import Path

import numpy as np
from ase import Atoms
from omegaconf import OmegaConf
from tqdm import tqdm
from transformers import PreTrainedTokenizerFast

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from autocata_core.utils.generation_utils import str_to_atoms


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args():
    p = argparse.ArgumentParser(description="Decode generated AutoCata pkl files and write valid structures.")
    p.add_argument("--config", default="config/config.yml", help="Path to the training config YAML file.")
    p.add_argument("--adsorbate", help="Adsorbate name. Overrides experiment.adsorbate in the config.")
    p.add_argument("--pkl-path", help="Generated pkl path. Defaults to generated_{adsorbate}.pkl.")
    p.add_argument("--tokenizer-path", help="Tokenizer path. Defaults to latest checkpoint, then base checkpoint.")
    p.add_argument("--output-dir", help="Analysis output directory.")
    p.add_argument("--max-samples", type=int, help="Maximum number of generated samples to process.")
    p.add_argument("--lat-idx", type=int, default=0, help="Lattice start index passed to str_to_atoms.")
    p.add_argument("--early-stop", action="store_true", help="Stop atom parsing early on overlaps.")
    p.add_argument("--skip-fail", action="store_true", help="Skip overlapped atoms during str_to_atoms.")
    p.add_argument("--write-xyz", dest="write_xyz", action="store_true", default=True)
    p.add_argument("--no-write-xyz", dest="write_xyz", action="store_false")
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


def checkpoint_sort_key(path):
    match = re.fullmatch(r"checkpoint-(\d+)", path.name)
    if match:
        return int(match.group(1))
    return -1


def find_latest_checkpoint(output_dir):
    output_dir = Path(output_dir)
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


def load_paths(config_path, args):
    params = OmegaConf.load(config_path)
    adsorbate = args.adsorbate or params.experiment.adsorbate
    params.experiment.adsorbate = adsorbate

    output_dir = resolve_project_path(format_template(params.paths.output_dir_template, adsorbate))
    generation_dir_name = OmegaConf.select(params, "paths.generation_dir_name", default="generated")
    analysis_dir_name = OmegaConf.select(params, "paths.analysis_dir_name", default="analysis")
    generated_name = f"generated_{adsorbate}.pkl"

    pkl_path = (
        resolve_project_path(args.pkl_path)
        if args.pkl_path
        else output_dir / generation_dir_name / generated_name
    )

    if args.tokenizer_path:
        tokenizer_path = resolve_project_path(args.tokenizer_path)
    else:
        latest_checkpoint = find_latest_checkpoint(output_dir)
        base_checkpoint = resolve_project_path(params.model_params.checkpoint_path)
        if latest_checkpoint is not None and has_tokenizer(latest_checkpoint):
            tokenizer_path = latest_checkpoint
        elif has_tokenizer(base_checkpoint):
            tokenizer_path = base_checkpoint
        else:
            tokenizer_path = resolve_project_path(f"data/tokenizer/{params.data_params.string_type}-tokenizer")

    output_path = (
        resolve_project_path(args.output_dir)
        if args.output_dir
        else output_dir / analysis_dir_name / pkl_path.stem
    )

    return params, pkl_path, tokenizer_path, output_path


def require_file(path, label):
    if not Path(path).is_file():
        raise FileNotFoundError(f"Missing {label}: {path_for_message(path)}")


def require_tokenizer(path):
    if not Path(path).is_dir() or not has_tokenizer(path):
        raise FileNotFoundError(f"Missing tokenizer files in: {path_for_message(path)}")


def parse_atoms_manual(atoms_str, cell):
    """Parse element/fractional-coordinate tokens into an ASE Atoms object."""
    toks = atoms_str.split()
    elems = []
    fracs = []

    i = 0
    n = len(toks)
    while i < n:
        token = toks[i]

        if token.startswith("<") and token.endswith(">"):
            i += 1
            continue

        if token.isalpha() and len(token) <= 2:
            if i + 3 >= n:
                break

            try:
                x = float(toks[i + 1])
                y = float(toks[i + 2])
                z = float(toks[i + 3])
            except (ValueError, IndexError):
                i += 1
                continue

            elems.append(token)
            fracs.append([x, y, z])
            i += 4
            continue

        i += 1

    if not elems:
        return None

    atoms = Atoms(symbols=elems, cell=cell, pbc=True)
    atoms.set_scaled_positions(np.array(fracs, dtype=float))
    return atoms


def tensor_like_to_ids(seq):
    if hasattr(seq, "detach"):
        values = seq.detach().cpu()
    else:
        values = seq

    if hasattr(values, "tolist"):
        values = values.tolist()

    while isinstance(values, list) and len(values) == 1 and isinstance(values[0], list):
        values = values[0]

    return values


def decode_sequence(tokenizer, seq):
    ids = tensor_like_to_ids(seq)
    text = tokenizer.decode(ids, skip_special_tokens=False)

    if "<eos>" in text:
        text = text.split("<eos>")[0]
    if text.startswith("<bos>"):
        text = text.replace("<bos>", "", 1).strip()

    return text.strip()


def write_summary(summary_path, stats):
    with open(summary_path, "w", encoding="utf-8") as fw:
        for key, value in stats.items():
            fw.write(f"{key}: {value}\n")


def main():
    args = parse_args()
    config_path = resolve_project_path(args.config)
    params, pkl_path, tokenizer_path, output_dir = load_paths(config_path, args)

    require_file(pkl_path, "generated pkl")
    require_tokenizer(tokenizer_path)

    print(f"[config] adsorbate={params.experiment.adsorbate}")
    print(f"[paths] pkl={path_for_message(pkl_path)}")
    print(f"[paths] tokenizer={path_for_message(tokenizer_path)}")
    print(f"[paths] output_dir={path_for_message(output_dir)}")

    with open(pkl_path, "rb") as f:
        gens = pickle.load(f)

    tokenizer = PreTrainedTokenizerFast.from_pretrained(str(tokenizer_path))
    max_samples = args.max_samples if args.max_samples is not None else len(gens)
    samples = gens[:max_samples]

    xyz_dir = output_dir / "xyz"
    os.makedirs(xyz_dir, exist_ok=True)

    bad_records = []
    stats = {
        "adsorbate": params.experiment.adsorbate,
        "pkl": path_for_message(pkl_path),
        "total_loaded": len(gens),
        "total_processed": len(samples),
        "valid_xyz": 0,
        "str_to_atoms_failed": 0,
        "manual_parse_failed": 0,
        "struct_invalid": 0,
        "generation_invalid": 0,
    }

    for idx, seq in enumerate(tqdm(samples, desc="checking")):
        atoms_str = decode_sequence(tokenizer, seq)
        tmp_atoms, struct_val, gen_val = str_to_atoms(
            atoms_str,
            lat_idx=args.lat_idx,
            skip_fail=args.skip_fail,
            early_stop=args.early_stop,
        )

        if not struct_val:
            stats["struct_invalid"] += 1
        if not gen_val:
            stats["generation_invalid"] += 1

        if tmp_atoms is None:
            stats["str_to_atoms_failed"] += 1
            bad_records.append((idx, f"str_to_atoms failed (struct_val={struct_val}, gen_val={gen_val})", atoms_str))
            continue

        manual_atoms = parse_atoms_manual(atoms_str, tmp_atoms.get_cell())
        if manual_atoms is None:
            stats["manual_parse_failed"] += 1
            bad_records.append((idx, "manual parse failed (no elems)", atoms_str))
            continue

        if args.write_xyz:
            manual_atoms.write(str(xyz_dir / f"sample_{idx}.xyz"))
        stats["valid_xyz"] += 1

    bad_path = output_dir / "bad_samples.txt"
    with open(bad_path, "w", encoding="utf-8") as fw:
        for idx, reason, atoms_str in bad_records:
            fw.write(f"{idx}\t{reason}\t{atoms_str}\n")

    summary_path = output_dir / "summary.txt"
    write_summary(summary_path, stats)

    print(f"[done] valid_xyz={stats['valid_xyz']} bad={len(bad_records)}")
    print(f"[done] summary={path_for_message(summary_path)}")
    print(f"[done] bad_samples={path_for_message(bad_path)}")
    if args.write_xyz:
        print(f"[done] xyz_dir={path_for_message(xyz_dir)}")


if __name__ == "__main__":
    main()
