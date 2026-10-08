#!/usr/bin/env python3
"""Single-point total-energy scoring, separate from OC20 workflow filtering."""
import argparse
import csv
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(PROJECT_ROOT))

from MLP_check.backends import DEFAULT_MODELS, build_calculator, package_versions


def project_path(value):
    path = Path(value)
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def parse_options(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="YAML file with an mlp_scoring mapping.")
    parser.add_argument("--backend", choices=tuple(DEFAULT_MODELS))
    parser.add_argument("--model", help="Explicit pretrained model name (may download on a real run).")
    parser.add_argument("--checkpoint", help="Local checkpoint instead of pretrained model.")
    parser.add_argument("--device", choices=("cpu", "cuda"))
    parser.add_argument("--dtype", choices=("float32", "float64"), help="MACE floating-point precision.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--xyz-dir", help="Recursively score XYZ files in this directory.")
    source.add_argument("--success-summary", help="Existing single/reaction workflow success CSV.")
    parser.add_argument("--output-dir", help="New output directory; existing nonempty directories are refused.")
    parser.add_argument("--max-files", type=int, help="Positive limit; default 5. Use --all-files for all inputs.")
    parser.add_argument("--all-files", action="store_true", help="Score all selected input structures.")
    parser.add_argument("--pbc", choices=("preserve", "slab", "periodic"))
    parser.add_argument("--dry-run", action="store_true", help="Validate paths and print plan; never load a model.")
    args = vars(parser.parse_args(argv))
    config = args.pop("config")
    options = {}
    if config:
        import yaml
        document = yaml.safe_load(project_path(config).read_text(encoding="utf-8"))
        if not isinstance(document, dict) or not isinstance(document.get("mlp_scoring"), dict):
            raise ValueError("Config must contain an mlp_scoring mapping.")
        options = dict(document["mlp_scoring"])
        unknown = set(options) - (set(args) - {"all_files", "dry_run"})
        if unknown:
            raise ValueError(f"Unknown mlp_scoring options: {sorted(unknown)}")
    if args["xyz_dir"]:
        options.pop("success_summary", None)
    if args["success_summary"]:
        options.pop("xyz_dir", None)
    if args["backend"] and args["backend"] != options.get("backend"):
        # A CLI backend switch must not inherit the other backend's model name.
        options.pop("model", None)
        options.pop("checkpoint", None)
    for key, value in args.items():
        if value is not None:
            options[key] = value
    backend = options.get("backend")
    if backend not in DEFAULT_MODELS:
        raise ValueError("Select backend mace or chgnet.")
    options.setdefault("device", "cpu")
    options.setdefault("pbc", "preserve")
    options.setdefault("dtype", "float64")
    options.setdefault("max_files", 5)
    options.setdefault("model", DEFAULT_MODELS[backend])
    if options["device"] not in ("cpu", "cuda") or options["pbc"] not in ("preserve", "slab", "periodic"):
        raise ValueError("Invalid device or pbc option.")
    if options["dtype"] not in ("float32", "float64"):
        raise ValueError("dtype must be float32 or float64.")
    if not isinstance(options["model"], str) or not options["model"]:
        raise ValueError("model must be a nonempty string.")
    if isinstance(options["max_files"], bool) or not isinstance(options["max_files"], int) or options["max_files"] <= 0:
        raise ValueError("max_files must be a positive integer; use --all-files for unlimited scoring.")
    if bool(options.get("xyz_dir")) == bool(options.get("success_summary")):
        raise ValueError("Supply exactly one xyz_dir or success_summary.")
    if not options.get("output_dir"):
        raise ValueError("output_dir is required.")
    options["output_dir"] = str(project_path(options["output_dir"]))
    if options.get("checkpoint"):
        options["checkpoint"] = str(project_path(options["checkpoint"]))
        if not Path(options["checkpoint"]).is_file():
            raise FileNotFoundError(options["checkpoint"])
    if backend == "chgnet" and options["pbc"] == "slab":
        raise ValueError("CHGNet uses periodic crystal graphs; use a 3D periodic vacuum cell, not slab PBC.")
    return options


def collect_inputs(options):
    if options.get("success_summary"):
        source = project_path(options["success_summary"])
        with source.open(newline="", encoding="utf-8-sig") as handle:
            records = list(csv.DictReader(handle))
        entries = []
        for index, row in enumerate(records, start=2):
            if row.get("passed", "true").strip().lower() not in ("", "true", "1", "yes"):
                continue
            value = row.get("success_xyz_file") or row.get("xyz_file") or row.get("source_xyz_file")
            if not value:
                raise ValueError(f"Missing structure path in {source}, line {index}.")
            entries.append((project_path(value), row))
    else:
        source = project_path(options["xyz_dir"])
        if not source.is_dir():
            raise NotADirectoryError(source)
        entries = [(p.resolve(), {}) for p in sorted(source.rglob("*.xyz"))]
    if not options["all_files"]:
        entries = entries[:options["max_files"]]
    if not entries:
        raise ValueError("No XYZ structures selected.")
    for path, _ in entries:
        if not path.is_file():
            raise FileNotFoundError(f"Missing input XYZ: {path}")
    return entries


def score_one(path, calculator, backend, pbc):
    import numpy as np
    from ase.io import read

    atoms = read(path)
    if len(atoms) == 0 or not np.isfinite(atoms.positions).all():
        raise ValueError("Structure must contain atoms with finite coordinates.")
    if pbc != "preserve":
        atoms.pbc = (True, True, pbc == "periodic")
    if not np.isfinite(atoms.cell).all():
        raise ValueError("Cell contains nonfinite values.")
    if np.any(atoms.pbc) and np.linalg.matrix_rank(np.asarray(atoms.cell)[atoms.pbc]) < int(sum(atoms.pbc)):
        raise ValueError("Periodic structure has an incomplete cell.")
    if backend == "chgnet" and (atoms.cell.rank != 3 or not all(atoms.pbc)):
        raise ValueError("CHGNet requires a full 3D-periodic cell; use --pbc periodic only with a suitable vacuum cell.")
    atoms.calc = calculator
    energy = float(atoms.get_potential_energy(apply_constraint=False))
    # Raw model forces must not be zeroed by FixAtoms or other ASE constraints.
    forces = np.asarray(atoms.get_forces(apply_constraint=False))
    if not math.isfinite(energy) or forces.shape != (len(atoms), 3) or not np.isfinite(forces).all():
        raise ValueError("Calculator returned nonfinite energy or invalid forces.")
    return {
        "natoms": len(atoms), "formula": atoms.get_chemical_formula(),
        "E_total_eV": energy, "E_per_atom_eV": energy / len(atoms),
        "F_rms_eV_A": float(np.sqrt(np.mean(forces ** 2))),
        "F_max_eV_A": float(np.linalg.norm(forces, axis=1).max()),
        "pbc_used": " ".join("T" if b else "F" for b in atoms.pbc),
    }


FIELDS = ["xyz_file", "sample_id", "material", "adsorbate", "natoms", "formula",
          "backend", "model", "energy_kind", "energy_unit", "force_unit",
          "E_total_eV", "E_per_atom_eV", "F_rms_eV_A", "F_max_eV_A", "pbc_used", "status", "error"]


def run(options, calculator_factory=None):
    entries = collect_inputs(options)
    output = Path(options["output_dir"])
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise FileExistsError(f"Use a new output directory: {output}")
    plan = {"options": options, "selected_count": len(entries),
            "energy_kind": "total", "energy_unit": "eV", "force_unit": "eV/Angstrom",
            "structures": [str(path) for path, _ in entries],
            "package_versions": package_versions(options["backend"])}
    if options["dry_run"]:
        print(json.dumps({"status": "dry_run", **plan}, indent=2))
        return 0
    factory = calculator_factory or build_calculator
    calculator = factory(backend=options["backend"], model=options["model"],
                         checkpoint=options.get("checkpoint"), device=options["device"], dtype=options["dtype"])
    output.mkdir(parents=True, exist_ok=True)
    # Exclusive creation additionally protects against concurrent writers.
    failures = 0
    with (output / "scores.csv").open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for path, metadata in entries:
            row = {"xyz_file": str(path), "sample_id": metadata.get("sample_id") or path.stem,
                   "material": metadata.get("material", ""), "adsorbate": metadata.get("adsorbate", ""),
                   "backend": options["backend"], "model": options.get("checkpoint") or options["model"],
                   "energy_kind": "total", "energy_unit": "eV", "force_unit": "eV/Angstrom",
                   "status": "success", "error": ""}
            try:
                row.update(score_one(path, calculator, options["backend"], options["pbc"]))
            except Exception as exc:
                row.update(status="failed", error=str(exc))
                failures += 1
            writer.writerow(row)
            handle.flush()
    report = {**plan, "status": "partial_failure" if failures else "success",
              "success_count": len(entries) - failures, "failure_count": failures,
              "created_utc": datetime.now(timezone.utc).isoformat(),
              "scores_csv": str(output / "scores.csv")}
    with (output / "scoring_report.json").open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    print(f"[done] scored={len(entries) - failures}/{len(entries)} output={output}")
    return 1 if failures else 0


def main(argv=None):
    try:
        return run(parse_options(argv))
    except (ValueError, OSError, RuntimeError, ImportError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
