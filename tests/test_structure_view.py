"""Lightweight geometry/render regression tests; no workflow or model imports."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from ase import Atoms
from ase.build import fcc111, fcc100, add_adsorbate, molecule
from ase.io import write

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'script'))
from structure_view import prepare_view, project_view, load_view
from render_xyz_preview import render_frame, render_row
from build_structure_gallery import copy_structures, write_html
from types import SimpleNamespace


class StructureViewTests(unittest.TestCase):
    def slab(self, builder=fcc111, layers=3, formula='OH'):
        atoms = builder('Cu', size=(3, 3, layers), vacuum=10)
        ads = molecule('H2O') if formula == 'H2O' else Atoms('OH', positions=[[0, 0, 0], [0, 0, 1]])
        add_adsorbate(atoms, ads, 2, position=(0.2, 0.3))
        # ASE slab layer tags differ from OC20 tags: generated XYZ uses no tags.
        atoms.set_tags(0)
        return atoms

    def check_view(self, atoms, formula='OH'):
        original = atoms.positions.copy()
        view, mask = prepare_view(atoms, {'adsorbate_formula': formula})
        np.testing.assert_array_equal(atoms.positions, original)
        np.testing.assert_allclose(atoms.get_all_distances(mic=True),
                                   view.get_all_distances(mic=True), atol=1e-8)
        # A reflection also preserves distances, so check handedness separately.
        if atoms.cell.rank == 3:
            self.assertAlmostEqual(np.linalg.det(atoms.cell), np.linalg.det(view.cell), places=7)
        self.assertGreater(view.positions[mask, 2].mean(), view.positions[~mask, 2].mean())
        for angle in range(0, 360, 15):
            p = project_view(view.positions, mask, angle)
            self.assertGreater(p[mask, 1].mean(), p[~mask, 1].mean())
            if view.positions[mask, 2].min() > view.positions[~mask, 2].max():
                self.assertGreater(p[mask, 1].min(), p[~mask, 1].max())
        return view, mask

    def test_faces_thickness_species_and_rotation(self):
        for builder in (fcc111, fcc100):
            for layers in (1, 3, 7):
                for formula in ('OH', 'H2O'):
                    for angle in (0, 73, 180):
                        with self.subTest(face=builder.__name__, layers=layers, formula=formula, angle=angle):
                            atoms = self.slab(builder, layers, formula)
                            atoms.rotate(angle, (1, 2, 3), rotate_cell=True)
                            self.check_view(atoms, formula)

    def test_periodic_split_and_skew_cell(self):
        atoms = self.slab()
        atoms.pbc = True
        atoms.cell[2] += atoms.cell[0] * 0.4
        atoms.positions += atoms.cell[2] * 0.77
        atoms.wrap()
        atoms.positions[-1] += atoms.cell[0] - atoms.cell[2]
        self.check_view(atoms)

    def test_bottom_adsorption(self):
        atoms = self.slab()
        atoms.positions[-2:, 2] = atoms.positions[:-2, 2].min() - np.array([2, 3])
        self.check_view(atoms)

    def test_single_atom_and_shared_slab_element(self):
        atoms = self.slab()[:-2]
        top = atoms.positions[:, 2].max()
        atoms += Atoms('Cu', positions=[[1, 1, top + 2]])
        self.check_view(atoms, 'Cu')
        atoms[-1].symbol = 'H'
        self.check_view(atoms, 'H')

    def test_no_cell_and_tags(self):
        atoms = self.slab()
        atoms.set_cell(np.zeros((3, 3)))
        atoms.pbc = False
        atoms.rotate(67, 'x')
        self.check_view(atoms)
        tags = np.zeros(len(atoms), int)
        tags[-2:] = 2
        atoms.set_tags(tags)
        shuffled = atoms[np.r_[len(atoms)-1, np.arange(len(atoms)-1)]]
        view, mask = prepare_view(shuffled, {})
        self.assertTrue(mask[0])
        self.assertEqual(mask.sum(), 2)

    def test_missing_or_invalid_identity(self):
        atoms = self.slab()
        for row in ({}, {'adsorbate': 'CH3'}):
            with self.assertRaises(ValueError):
                prepare_view(atoms, row)

    def test_files_raster_animation_gallery(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / 'input.xyz'
            atoms = self.slab()
            write(source, atoms)
            original = source.read_bytes()
            row = {'_source': source, '_adsorbate': 'OH', 'adsorbate_formula': 'OH',
                   'success_xyz_file': str(source), 'sample_id': 'test', 'material': 'Cu'}
            view, mask = load_view(source, row)
            self.assertEqual(render_frame(view, row, 30, 720, 540, mask).size, (720, 540))
            args = SimpleNamespace(image_width=720, image_height=540, no_gif=False,
                                   frames=4, gif_duration_ms=160)
            png, gif, _ = render_row(row, 1, folder, args)
            self.assertTrue(png.is_file() and gif.is_file())
            manifest = copy_structures([row], folder / 'structures')
            write_html(folder / 'index.html', 'Test', manifest)
            self.assertNotEqual(manifest[0]['display_xyz'], manifest[0]['xyz'])
            text = (folder / 'index.html').read_text()
            embedded = text.split('type="application/json">')[1].split('</script>')[0]
            self.assertEqual(json.loads(embedded), manifest)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual((folder / 'structures/0001_test.xyz').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
