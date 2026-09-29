"""Adapt one MLP or VASP relaxed frame to the shared reliability classifier."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import read_extxyz_structure  # noqa: E402
from relaxed_structure_classification import classify  # noqa: E402


def read_poscar(path: Path) -> dict[str, Any]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if len(lines) < 8:
        raise ValueError(f"POSCAR is truncated: {path}")
    scale = float(lines[1])
    cell = [[float(value) * scale for value in lines[index].split()] for index in range(2, 5)]
    symbols = lines[5].split()
    counts = [int(value) for value in lines[6].split()]
    if len(symbols) != len(counts) or not symbols:
        raise ValueError(f"POSCAR element/count lines are invalid: {path}")
    cursor = 7
    if lines[cursor].lower().startswith("selective"):
        cursor += 1
    direct = lines[cursor].lower().startswith("direct")
    if direct or lines[cursor].lower().startswith("cart"):
        cursor += 1
    total = sum(counts)
    positions = [[float(value) for value in lines[cursor + index].split()[:3]] for index in range(total)]
    if direct:
        positions = [[sum(positions[index][axis] * cell[axis][component] for axis in range(3)) for component in range(3)] for index in range(total)]
    expanded = [symbol for symbol, count in zip(symbols, counts) for _ in range(count)]
    return {"symbols": expanded, "positions": positions, "cell": cell}


def read_structure(path: Path) -> dict[str, Any]:
    if path.suffix.lower() in {".xyz", ".extxyz"}:
        parsed = read_extxyz_structure(path)
        return {"symbols": parsed["species"], "positions": [list(item) for item in parsed["positions"]], "cell": parsed["lattice"]}
    return read_poscar(path)


def normalize(source_type: str, initial_path: Path, final_path: Path, result_path: Path, policy: dict[str, Any]) -> dict[str, Any]:
    initial = read_structure(initial_path)
    final = read_structure(final_path)
    result = json.loads(result_path.read_text(encoding="utf-8-sig"))
    if source_type == "mlp":
        energy = result.get("E_adsorption_eV")
        status = result.get("status")
        converged = result.get("converged")
    elif source_type == "vasp":
        energy = result.get("energy_eV")
        status = result.get("status", "completed")
        converged = result.get("ionic_converged")
    else:
        raise ValueError("source_type must be mlp or vasp")
    record = {
        "source_type": source_type,
        "status": status,
        "converged": converged,
        "energy_eV": energy,
        "symbols": final["symbols"],
        "initial_symbols": initial["symbols"],
        "initial_positions": initial["positions"],
        "final_positions": final["positions"],
        "cell": final["cell"],
        "pbc": policy["pbc"],
    }
    classified = classify(record, policy)
    classified.update({"initial_path": str(initial_path), "final_path": str(final_path), "result_path": str(result_path)})
    return classified


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-type", choices=("mlp", "vasp"), required=True)
    parser.add_argument("--initial", type=Path, required=True)
    parser.add_argument("--final", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True, help="MLP result.json or VASP checkpoint/result JSON")
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    policy = json.loads(args.policy.read_text(encoding="utf-8-sig"))
    result = normalize(args.source_type, args.initial, args.final, args.result, policy)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
