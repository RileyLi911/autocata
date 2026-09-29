from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from common import read_extxyz_structure, sha256_file, validate_poscar, write_poscar


def run_test(root: Path) -> None:
    source = root / "relaxed_structure.extxyz"
    source.write_text(
        '2\n'
        'Lattice="3 0 0 0 3 0 0 0 10" Properties=species:S:1:pos:R:3:move_mask:L:3 pbc="T T T"\n'
        'Cu 0 0 0 F F F\n'
        'O 0 0 2 T T T\n',
        encoding="utf-8",
    )
    structure = read_extxyz_structure(source)
    target = root / "POSCAR"
    write_poscar(structure, target, "fixture structure_index=0")
    parsed = validate_poscar(target)
    assert parsed["atom_count"] == 2
    assert parsed["elements"] == ["Cu", "O"]
    assert len(sha256_file(target)) == 64
    assert "structure_index=0" in target.read_text(encoding="utf-8")
    assert "F F F" in target.read_text(encoding="utf-8")
    print("Relax POSCAR self-test passed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-root", type=Path)
    args = parser.parse_args()
    if args.fixture_root:
        args.fixture_root.mkdir(parents=True, exist_ok=True)
        run_test(args.fixture_root.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="relax-poscar-") as temporary:
            run_test(Path(temporary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
