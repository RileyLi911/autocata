from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from common import read_structure_frames


def write_poscar(path: Path, x_shift: float = 0.0) -> None:
    path.write_text(
        "fixture\n1.0\n"
        "4 0 0\n0 4 0\n0 0 6\n"
        "Cu O\n1 1\nCartesian\n"
        f"1 1 1\n{2 + x_shift} 2 2\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        from ase import Atoms
        from ase.io import write
    except ImportError:
        print("structure input self-test skipped: ASE unavailable")
        return 0

    root = args.fixture_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        xyz = root / "frames.extxyz"
        xyz.write_text(
            "2\nLattice=\"4 0 0 0 4 0 0 0 6\" Properties=species:S:1:pos:R:3 pbc=\"T T T\"\n"
            "Cu 0 0 0\nO 1 1 1\n",
            encoding="utf-8",
        )
        frames, provenance = read_structure_frames(xyz)
        assert len(frames) == 1 and provenance["file_type"] == "extxyz"
        assert provenance["source_files"][0]["sha256"]

        poscar = root / "POSCAR"
        write_poscar(poscar)
        frames, provenance = read_structure_frames(poscar)
        assert len(frames) == 1 and provenance["file_type"] == "poscar"
        vasp = root / "structure.vasp"
        write_poscar(vasp)
        frames, provenance = read_structure_frames(vasp)
        assert len(frames) == 1 and provenance["file_type"] == "poscar"
        assert frames[0].get_chemical_formula() == "CuO"

        cif = root / "one.cif"
        write(cif, Atoms("CuO", positions=[(0, 0, 0), (1, 1, 1)], cell=[4, 4, 6], pbc=True), format="cif")
        frames, provenance = read_structure_frames(cif)
        assert len(frames) == 1 and provenance["file_type"] == "cif"

        folder = root / "frames"
        folder.mkdir()
        write_poscar(folder / "02.POSCAR", 0.2)
        write_poscar(folder / "01.POSCAR", 0.1)
        frames, provenance = read_structure_frames(folder)
        assert len(frames) == 2
        assert provenance["input_mode"] == "directory_sorted_filename"
        assert [Path(item["path"]).name for item in provenance["source_files"]] == ["01.POSCAR", "02.POSCAR"]

        mixed = root / "mixed"
        mixed.mkdir()
        write_poscar(mixed / "01.POSCAR")
        write(cif := mixed / "02.cif", Atoms("CuO", positions=[(0, 0, 0), (1, 1, 1)], cell=[4, 4, 6], pbc=True), format="cif")
        try:
            read_structure_frames(mixed)
        except ValueError as exc:
            assert "mixed structure formats" in str(exc)
        else:
            raise AssertionError("mixed structure directory was accepted")

        print("structure input self-test passed")
        return 0
    finally:
        # The caller owns cleanup of the explicit fixture root.
        pass


if __name__ == "__main__":
    raise SystemExit(main())
