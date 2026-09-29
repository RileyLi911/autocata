from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from relaxed_structure_classification import classify  # noqa: E402


POLICY = {
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


def base(source_type: str = "mlp") -> dict:
    return {
        "source_type": source_type,
        "status": "success",
        "converged": True,
        "energy_eV": -1.0,
        "symbols": ["Cu", "Cu", "Cu", "N", "O", "O", "O"],
        "initial_symbols": ["Cu", "Cu", "Cu", "N", "O", "O", "O"],
        "initial_positions": [[0, 0, 0], [3, 0, 0], [0, 3, 0], [1, 1, 1.8], [1.8, 1, 1.8], [1, 1.8, 1.8], [1, 1, 2.6]],
        "final_positions": [[0, 0, 0], [3, 0, 0], [0, 3, 0], [1, 1, 1.8], [1.8, 1, 1.8], [1, 1.8, 1.8], [1, 1, 2.6]],
        "cell": [[10, 0, 0], [0, 10, 0], [0, 0, 20]],
        "pbc": [True, True, False],
    }


def assert_category(record: dict, expected: str) -> None:
    actual = classify(record, POLICY)["reliability_category"]
    assert actual == expected, (expected, actual)


def main() -> None:
    assert_category(base(), "Intact and converged")
    record = base(); record["converged"] = False; assert_category(record, "Unconverged")
    record = base(); record["final_positions"] = copy.deepcopy(record["initial_positions"]); record["final_positions"][4][2] = 4.0; assert_category(record, "Dissociated")
    record = base(); record["final_positions"] = copy.deepcopy(record["initial_positions"]); record["final_positions"][3][2] = 4.0; record["final_positions"][4][2] = 4.8; record["final_positions"][5][2] = 4.0; record["final_positions"][6][2] = 4.8; assert_category(record, "Detached")
    record = base(); record["converged"] = False; record["final_positions"][1] = [0, 0, 0]; assert_category(record, "Invalid")
    record = base(); record["final_positions"][0][2] = 0.05; assert_category(record, "Intact and converged")
    record = base(); record["final_positions"][0][2] = 0.051; assert_category(record, "Invalid")
    result = classify(record, POLICY)
    assert result["fixed_atom_indices"] == [0, 1, 2]
    assert result["fixed_atom_displacement_violation"] is True
    record = base(); record["final_positions"][0][2] = 0.051; record["converged"] = False; assert_category(record, "Invalid")
    record = base("vasp"); assert_category(record, "Intact and converged")
    record = base(); record["status"] = "bad-status"; assert_category(record, "Invalid")
    print("relaxed structure classification self-test passed")


if __name__ == "__main__":
    main()
