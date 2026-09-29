from __future__ import annotations

import json
import tempfile
from pathlib import Path

from classify_relaxed_frame import normalize


POLICY = {
    "pbc": [True, True, False],
    "adsorbate_indices": [3, 4, 5, 6],
    "surface_indices": [0, 1, 2],
    "monitored_bonds": [[3, 4], [3, 5], [3, 6]],
    "covalent_radii": {"Cu": 1.32, "N": 0.71, "O": 0.66},
    "severe_overlap_covalent_ratio_threshold": 0.55,
    "catastrophic_surface_max_displacement_A": 5.0,
    "fixed_atom_max_displacement_A": 0.05,
    "dissociation_bond_distance_A": 1.8,
    "detached_final_contact_distance_A": 3.5,
    "detached_contact_increase_A": 0.6,
}


def poscar(path: Path, positions: list[list[float]]) -> None:
    lines = [
        "fixture", "1.0", "10 0 0", "0 10 0", "0 0 20", "Cu N O", "3 1 3", "Cartesian",
    ]
    lines.extend("%s %s %s" % tuple(item) for item in positions)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    initial_positions = [[0, 0, 0], [3, 0, 0], [0, 3, 0], [1, 1, 1.8], [1.8, 1, 1.8], [1, 1.8, 1.8], [1, 1, 2.6]]
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
        root = Path(folder)
        initial = root / "POSCAR"
        final = root / "CONTCAR"
        result = root / "checkpoint.json"
        poscar(initial, initial_positions)
        poscar(final, initial_positions)
        result.write_text(json.dumps({"energy_eV": -1.0, "ionic_converged": True, "status": "completed"}), encoding="utf-8")
        output = normalize("vasp", initial, final, result, POLICY)
        assert output["source_type"] == "vasp"
        assert output["reliability_category"] == "Intact and converged", output
    print("relaxed frame adapter self-test passed")


if __name__ == "__main__":
    main()
