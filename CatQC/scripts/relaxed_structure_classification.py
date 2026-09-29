"""Source-agnostic five-class classification for relaxed slab+adsorbate frames.

The classifier intentionally accepts a normalized JSON record instead of an
MLP- or VASP-specific result format.  Adapters should map their native output
to this record before classification.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


CATEGORIES = ("Invalid", "Unconverged", "Dissociated", "Detached", "Intact and converged")
ACCEPTED_STATUS = {"success", "ok", "completed", "complete", "unconverged"}


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _vec(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("vector must have three numeric components")
    result = [float(item) for item in value]
    if not all(math.isfinite(item) for item in result):
        raise ValueError("vector contains a nonfinite value")
    return result


def _matrix(value: Any) -> list[list[float]]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("cell must contain three vectors")
    return [_vec(row) for row in value]


def _det(cell: list[list[float]]) -> float:
    a, b, c = cell
    return (
        a[0] * (b[1] * c[2] - b[2] * c[1])
        - a[1] * (b[0] * c[2] - b[2] * c[0])
        + a[2] * (b[0] * c[1] - b[1] * c[0])
    )


def _inverse(cell: list[list[float]]) -> list[list[float]]:
    a, b, c = cell
    determinant = _det(cell)
    if abs(determinant) < 1e-12:
        raise ValueError("cell is singular")
    cofactors = [
        [b[1] * c[2] - b[2] * c[1], b[2] * c[0] - b[0] * c[2], b[0] * c[1] - b[1] * c[0]],
        [a[2] * c[1] - a[1] * c[2], a[0] * c[2] - a[2] * c[0], a[1] * c[0] - a[0] * c[1]],
        [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]],
    ]
    return [[cofactors[row][col] / determinant for row in range(3)] for col in range(3)]


def _matmul(matrix: list[list[float]], vector: list[float]) -> list[float]:
    return [sum(row[index] * vector[index] for index in range(3)) for row in matrix]


def _row_matmul(vector: list[float], matrix: list[list[float]]) -> list[float]:
    return [sum(vector[index] * matrix[index][axis] for index in range(3)) for axis in range(3)]


def _cart_from_fractional(cell: list[list[float]], fractional: list[float]) -> list[float]:
    return [sum(fractional[index] * cell[index][axis] for index in range(3)) for axis in range(3)]


def minimum_image_delta(first: list[float], second: list[float], cell: list[list[float]], pbc: list[bool]) -> list[float]:
    delta = [first[index] - second[index] for index in range(3)]
    fractional = _row_matmul(delta, _inverse(cell))
    for index, periodic in enumerate(pbc):
        if periodic:
            fractional[index] -= round(fractional[index])
    return _cart_from_fractional(cell, fractional)


def _distance(first: list[float], second: list[float], cell: list[list[float]], pbc: list[bool]) -> float:
    delta = minimum_image_delta(first, second, cell, pbc)
    return math.sqrt(sum(value * value for value in delta))


def _bottom_layer_indices(positions: list[list[float]], surface_indices: list[int], tolerance: float) -> list[int]:
    """Return atoms in the prescribed bottom one/two slab layers."""
    ordered = sorted((index for index in surface_indices if 0 <= index < len(positions)), key=lambda index: positions[index][2])
    layers: list[list[int]] = []
    centers: list[float] = []
    for index in ordered:
        z_value = positions[index][2]
        if not layers or abs(z_value - centers[-1]) > tolerance:
            layers.append([index])
            centers.append(z_value)
        else:
            layers[-1].append(index)
            centers[-1] = sum(positions[item][2] for item in layers[-1]) / len(layers[-1])
    fixed_layer_count = 1 if len(layers) <= 5 else 2
    return sorted(index for layer in layers[:fixed_layer_count] for index in layer)


def _normalise_status(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def classify(record: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Classify one normalized MLP/VASP relaxed slab+adsorbate record."""
    reasons: list[str] = []
    source_type = str(record.get("source_type") or "").strip().lower()
    status = _normalise_status(record.get("status"))
    converged = _bool(record.get("converged"))
    if converged is None and "ionic_converged" in record:
        converged = _bool(record.get("ionic_converged"))

    initial = record.get("initial_positions")
    final = record.get("final_positions")
    symbols = record.get("symbols")
    initial_symbols = record.get("initial_symbols", symbols)
    cell = record.get("cell")
    pbc = record.get("pbc")
    structure_valid = True
    if source_type not in {"mlp", "vasp"}:
        reasons.append("source_type must be mlp or vasp")
    if status not in ACCEPTED_STATUS:
        reasons.append("status is missing or unsupported")
    if not _finite(record.get("energy_eV")):
        reasons.append("energy_eV is missing or nonfinite")
    if not isinstance(initial, list) or not isinstance(final, list) or len(initial) != len(final) or not initial:
        reasons.append("initial/final structure is missing or has inconsistent atom count")
    if not isinstance(symbols, list) or not isinstance(initial_symbols, list) or symbols != initial_symbols:
        reasons.append("relaxed atom symbols/order differ from the initial structure")
    if not isinstance(cell, list) or not isinstance(pbc, list) or len(pbc) != 3:
        reasons.append("cell or pbc is missing")
    try:
        cell_matrix = _matrix(cell)
        pbc_flags = [bool(value) for value in pbc]
        initial_positions = [_vec(value) for value in initial]
        final_positions = [_vec(value) for value in final]
        if abs(_det(cell_matrix)) < 1e-12:
            reasons.append("cell is singular")
    except (TypeError, ValueError, IndexError) as exc:
        reasons.append(f"invalid structure geometry: {exc}")
        cell_matrix = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        pbc_flags = [False, False, False]
        initial_positions = []
        final_positions = []

    n_atoms = len(final_positions)
    adsorbate_indices = [int(index) for index in policy.get("adsorbate_indices", [])]
    surface_indices = [int(index) for index in policy.get("surface_indices", [])]
    monitored_bonds = policy.get("monitored_bonds", [])
    invalid_geometry = False
    min_ratio = math.inf
    min_pair: list[int] = []
    max_bond = math.nan
    final_contact = math.nan
    initial_contact = math.nan
    contact_increase = math.nan
    fixed_atom_indices: list[int] = []
    fixed_atom_max_displacement = math.nan
    fixed_atom_displacement_violation: bool | None = None
    if n_atoms and len(initial_positions) == n_atoms:
        radii = policy.get("covalent_radii", {})
        overlap_threshold = float(policy.get("severe_overlap_covalent_ratio_threshold", 0.55))
        for first in range(n_atoms):
            for second in range(first + 1, n_atoms):
                denominator = float(radii.get(symbols[first], 0.0)) + float(radii.get(symbols[second], 0.0))
                if denominator <= 0:
                    continue
                distance = _distance(final_positions[first], final_positions[second], cell_matrix, pbc_flags)
                ratio = distance / denominator
                if ratio < min_ratio:
                    min_ratio = ratio
                    min_pair = [first, second]
                if ratio < overlap_threshold:
                    invalid_geometry = True
        if surface_indices:
            displacements = [
                minimum_image_delta(final_positions[index], initial_positions[index], cell_matrix, pbc_flags)
                for index in surface_indices
                if 0 <= index < n_atoms
            ]
            if displacements:
                mean = [sum(item[axis] for item in displacements) / len(displacements) for axis in range(3)]
                max_displacement = max(
                    math.sqrt(sum((item[axis] - mean[axis]) ** 2 for axis in range(3)))
                    for item in displacements
                )
                if max_displacement > float(policy.get("catastrophic_surface_max_displacement_A", 5.0)):
                    invalid_geometry = True
            else:
                max_displacement = math.nan
            try:
                fixed_atom_indices = _bottom_layer_indices(
                    initial_positions, surface_indices, float(policy.get("layer_tolerance_A", 0.2))
                )
                if not fixed_atom_indices:
                    reasons.append("unable to determine fixed bottom-layer atoms")
                else:
                    fixed_displacements = [
                        math.sqrt(sum(value * value for value in minimum_image_delta(
                            final_positions[index], initial_positions[index], cell_matrix, pbc_flags)))
                        for index in fixed_atom_indices
                    ]
                    fixed_atom_max_displacement = max(fixed_displacements)
                    tolerance = float(policy.get("fixed_atom_max_displacement_A", 0.05))
                    fixed_atom_displacement_violation = fixed_atom_max_displacement > tolerance + 1e-12
                    if fixed_atom_displacement_violation:
                        reasons.append(
                            f"fixed bottom-layer atom displacement exceeds {tolerance:g} A"
                        )
                        invalid_geometry = True
            except (TypeError, ValueError, IndexError) as exc:
                reasons.append(f"fixed bottom-layer displacement check failed: {exc}")
        else:
            max_displacement = math.nan
            reasons.append("surface_indices are required to determine fixed bottom-layer atoms")
    else:
        max_displacement = math.nan

    if invalid_geometry:
        reasons.append("severe atomic overlap or catastrophic surface displacement")
    if reasons:
        category = "Invalid"
    elif not converged or status == "unconverged":
        category = "Unconverged"
    else:
        bond_distances = []
        for bond in monitored_bonds:
            first, second = int(bond[0]), int(bond[1])
            bond_distances.append(_distance(final_positions[first], final_positions[second], cell_matrix, pbc_flags))
        max_bond = max(bond_distances) if bond_distances else math.nan
        if bond_distances and max_bond > float(policy.get("dissociation_bond_distance_A", math.inf)):
            category = "Dissociated"
        else:
            adsorbate_slab_distances = [
                _distance(final_positions[ads], final_positions[slab], cell_matrix, pbc_flags)
                for ads in adsorbate_indices for slab in surface_indices
            ]
            final_contact = min(adsorbate_slab_distances) if adsorbate_slab_distances else math.nan
            initial_contacts = [
                _distance(initial_positions[ads], initial_positions[slab], cell_matrix, pbc_flags)
                for ads in adsorbate_indices for slab in surface_indices
            ]
            initial_contact = min(initial_contacts) if initial_contacts else math.nan
            contact_increase = final_contact - initial_contact if _finite(final_contact) and _finite(initial_contact) else math.nan
            detached = (
                _finite(final_contact)
                and final_contact > float(policy.get("detached_final_contact_distance_A", math.inf))
                and _finite(contact_increase)
                and contact_increase > float(policy.get("detached_contact_increase_A", math.inf))
            )
            category = "Detached" if detached else "Intact and converged"

    return {
        "source_type": source_type,
        "reliability_category": category,
        "invalid_reason": "; ".join(reasons),
        "status": status,
        "converged": converged,
        "min_pair_covalent_ratio": None if math.isinf(min_ratio) else min_ratio,
        "min_pair_atoms": min_pair,
        "slab_max_displacement_A": None if not _finite(max_displacement) else max_displacement,
        "fixed_atom_indices": fixed_atom_indices,
        "fixed_atom_max_displacement_A": None if not _finite(fixed_atom_max_displacement) else fixed_atom_max_displacement,
        "fixed_atom_displacement_tolerance_A": float(policy.get("fixed_atom_max_displacement_A", 0.05)),
        "fixed_atom_displacement_violation": fixed_atom_displacement_violation,
        "max_monitored_bond_distance_A": None if not _finite(max_bond) else max_bond,
        "final_adsorbate_slab_distance_A": None if not _finite(final_contact) else final_contact,
        "initial_adsorbate_slab_distance_A": None if not _finite(initial_contact) else initial_contact,
        "adsorbate_slab_distance_increase_A": None if not _finite(contact_increase) else contact_increase,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path, help="normalized input JSON")
    parser.add_argument("policy", type=Path, help="classification policy JSON")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    record = json.loads(args.record.read_text(encoding="utf-8-sig"))
    policy = json.loads(args.policy.read_text(encoding="utf-8-sig"))
    result = classify(record, policy)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
