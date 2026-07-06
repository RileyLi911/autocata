#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
from pathlib import Path

from fairchem.core.common.relaxation.ase_utils import OCPCalculator
from omegaconf import OmegaConf


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_DIR = Path(__file__).resolve().parent / "MLP_model"


def parse_args():
    p = argparse.ArgumentParser(description="Prepare or validate a local fairchem OC20 MLP checkpoint cache.")
    p.add_argument("--config", default="config/config.yml", help="Path to the project config YAML file.")
    p.add_argument("--model-name", help="fairchem pretrained model name.")
    p.add_argument("--model-cache", help="Local directory for downloaded MLP checkpoints.")
    p.add_argument("--device", default="cpu", choices=["cpu", "cuda"], help="Device used when initializing the calculator.")
    return p.parse_args()


def resolve_project_path(path):
    path = Path(path)
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


def resolve_path(path):
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


def main():
    args = parse_args()
    params = OmegaConf.load(resolve_project_path(args.config))
    model_name = args.model_name or OmegaConf.select(
        params,
        "mlp.model_name",
        default="CGCNN-S2EF-OC20-200k",
    )
    model_cache = resolve_path(
        args.model_cache or OmegaConf.select(params, "mlp.model_cache", default=str(DEFAULT_MODEL_DIR))
    )
    model_cache.mkdir(parents=True, exist_ok=True)

    print(f"[mlp] model_name={model_name}")
    print(f"[mlp] model_cache={path_for_message(model_cache)}")
    calc = OCPCalculator(
        model_name=model_name,
        local_cache=str(model_cache),
        cpu=args.device == "cpu",
    )
    print("[done] calculator ready")
    print(calc)


if __name__ == "__main__":
    main()
