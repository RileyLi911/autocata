"""No model packages or weights required: fake ASE calculators and mocked APIs."""
import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from ase import Atoms
from ase.calculators.calculator import Calculator, all_changes
from ase.constraints import FixAtoms
from ase.io import write

from MLP_check import backends
from MLP_check.score_structures import collect_inputs, parse_options, run, score_one


class FakeCalculator(Calculator):
    implemented_properties = ['energy', 'forces']

    def calculate(self, atoms=None, properties=None, system_changes=all_changes):
        super().calculate(atoms, properties, system_changes)
        self.results = {'energy': -8.0, 'forces': np.tile([3.0, 4.0, 0.0], (len(atoms), 1))}


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'xyz'
        self.source.mkdir()
        self.xyz = self.source / 'a.xyz'
        atoms = Atoms('CuH', positions=[[0, 0, 0], [0, 0, 2]], cell=[8, 8, 20], pbc=True)
        atoms.set_constraint(FixAtoms(indices=[0]))
        write(self.xyz, atoms)
        self.output = self.root / 'scores'

    def options(self, *extra):
        return parse_options(['--backend', 'mace', '--xyz-dir', str(self.source),
                              '--output-dir', str(self.output), *extra])

    def test_dry_run_does_not_load_model_or_write(self):
        factory = Mock(side_effect=AssertionError('must not load'))
        with redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(run(self.options('--dry-run'), factory), 0)
        self.assertEqual(json.loads(stream.getvalue())['selected_count'], 1)
        factory.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_scores_units_constraints_and_original_preserved(self):
        original = self.xyz.read_bytes()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(run(self.options(), Mock(return_value=FakeCalculator())), 0)
        with (self.output / 'scores.csv').open() as handle:
            row = next(csv.DictReader(handle))
        self.assertNotIn('E_pred', row)
        self.assertEqual(float(row['E_total_eV']), -8)
        self.assertEqual(float(row['E_per_atom_eV']), -4)
        self.assertAlmostEqual(float(row['F_rms_eV_A']), np.sqrt(25 / 3))
        self.assertEqual(float(row['F_max_eV_A']), 5)
        self.assertEqual(row['pbc_used'], 'T T T')
        self.assertEqual(self.xyz.read_bytes(), original)
        report = json.loads((self.output / 'scoring_report.json').read_text())
        self.assertEqual(report['success_count'], 1)
        self.assertIn('mace-torch', report['package_versions'])

    def test_bad_structure_recorded_and_later_inputs_scored(self):
        (self.source / '0_invalid.xyz').write_text('not XYZ')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(run(self.options(), Mock(return_value=FakeCalculator())), 1)
        report = json.loads((self.output / 'scoring_report.json').read_text())
        self.assertEqual((report['failure_count'], report['success_count']), (1, 1))

    def test_refuses_overwrite_before_model_loading(self):
        self.output.mkdir()
        (self.output / 'original.txt').write_text('keep')
        factory = Mock()
        with self.assertRaises(FileExistsError):
            run(self.options(), factory)
        factory.assert_not_called()

    def test_nonfinite_calculator_output_rejected(self):
        calc = FakeCalculator()
        calc.get_potential_energy = Mock(return_value=float('nan'))
        with self.assertRaisesRegex(ValueError, 'nonfinite'):
            score_one(self.xyz, calc, 'mace', 'preserve')

    def test_chgnet_total_energy_not_multiplied_twice(self):
        result = score_one(self.xyz, FakeCalculator(), 'chgnet', 'preserve')
        self.assertEqual(result['E_total_eV'], -8)
        self.assertEqual(result['E_per_atom_eV'], -4)

    def test_chgnet_partial_periodicity_rejected(self):
        with self.assertRaisesRegex(ValueError, '3D-periodic'):
            score_one(self.xyz, FakeCalculator(), 'chgnet', 'slab')

    def test_input_csv_and_recursive_directory(self):
        nested = self.source / 'OH'
        nested.mkdir()
        (nested / 'a.xyz').write_bytes(self.xyz.read_bytes())
        self.assertEqual(len(collect_inputs(self.options('--all-files'))), 2)
        summary = self.root / 'success.csv'
        with summary.open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=['success_xyz_file', 'adsorbate', 'passed'])
            writer.writeheader()
            writer.writerow({'success_xyz_file': str(self.xyz), 'adsorbate': 'H', 'passed': 'true'})
            writer.writerow({'success_xyz_file': 'does-not-exist.xyz', 'passed': 'false'})
        opts = parse_options(['--backend', 'chgnet', '--success-summary', str(summary), '--output-dir', str(self.output)])
        entries = collect_inputs(opts)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][1]['adsorbate'], 'H')

    def test_limits_and_missing_inputs(self):
        with self.assertRaises(ValueError):
            self.options('--max-files', '0')
        with self.assertRaises(FileNotFoundError):
            self.options('--checkpoint', str(self.root / 'absent.model'))

    def test_yaml_cli_override(self):
        config = self.root / 'config.yml'
        config.write_text('mlp_scoring:\n  backend: mace\n  model: medium-mpa-0\n'
                          f'  xyz_dir: {self.source}\n  output_dir: {self.output}\n  max_files: 3\n')
        opts = parse_options(['--config', str(config), '--device', 'cpu', '--max-files', '1', '--dry-run'])
        self.assertEqual(opts['max_files'], 1)
        self.assertEqual(len(collect_inputs(opts)), 1)
        switched = parse_options(['--config', str(config), '--backend', 'chgnet'])
        self.assertEqual(switched['model'], '0.3.0')


class AdapterTests(unittest.TestCase):
    def modules(self):
        return {
            'torch': SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)),
            'mace.calculators': SimpleNamespace(MACECalculator=Mock(), mace_mp=Mock()),
            'chgnet.model.model': SimpleNamespace(CHGNet=SimpleNamespace(load=Mock(), from_file=Mock())),
            'chgnet.model.dynamics': SimpleNamespace(CHGNetCalculator=Mock()),
        }

    def test_pretrained_constructor_arguments(self):
        modules = self.modules()
        with patch.object(backends, 'import_module', side_effect=modules.__getitem__):
            backends.build_calculator('mace', 'medium-mpa-0')
            modules['mace.calculators'].mace_mp.assert_called_once_with(
                model='medium-mpa-0', device='cpu', default_dtype='float64', dispersion=False)
            backends.build_calculator('chgnet', '0.3.0')
            model = modules['chgnet.model.model'].CHGNet
            model.load.assert_called_once_with(model_name='0.3.0', use_device='cpu')
            modules['chgnet.model.dynamics'].CHGNetCalculator.assert_called_once_with(
                model=model.load.return_value, use_device='cpu')

    def test_local_checkpoints_skip_pretrained_loaders(self):
        modules = self.modules()
        with tempfile.NamedTemporaryFile() as checkpoint:
            with patch.object(backends, 'import_module', side_effect=modules.__getitem__):
                backends.build_calculator('mace', 'unused', checkpoint.name)
                modules['mace.calculators'].MACECalculator.assert_called_once_with(
                    model_paths=checkpoint.name, device='cpu', default_dtype='float64')
                modules['mace.calculators'].mace_mp.assert_not_called()
                backends.build_calculator('chgnet', 'unused', checkpoint.name)
                modules['chgnet.model.model'].CHGNet.from_file.assert_called_once_with(checkpoint.name)
                modules['chgnet.model.model'].CHGNet.load.assert_not_called()

    def test_missing_dependency_and_unavailable_cuda(self):
        with patch.object(backends, 'import_module', side_effect=ImportError('missing')):
            with self.assertRaisesRegex(RuntimeError, 'separate environment'):
                backends.build_calculator('mace', 'medium-mpa-0')
        with patch.object(backends, 'import_module', side_effect=self.modules().__getitem__):
            with self.assertRaisesRegex(RuntimeError, 'CUDA'):
                backends.build_calculator('chgnet', '0.3.0', device='cuda')


if __name__ == '__main__':
    unittest.main()
