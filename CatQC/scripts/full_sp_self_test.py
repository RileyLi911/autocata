"""Full SP coverage, Relax cohort selection, repair and report freshness tests."""
from pathlib import Path
import tempfile
from common import read_csv, load_json, atomic_write_json
from model_selection_report_self_test import fixture, csv_file, ranking, write_reviewable
from model_selection_report import build_evidence, validate_report, EVIDENCE


def main():
    with tempfile.TemporaryDirectory(prefix='catqc-full-sp-') as directory:
        base = Path(directory)
        root = fixture(base/'cohort', bad=True)
        ranking(root, '--reliability-threshold', '0.5')
        evidence = build_evidence(root)
        assert evidence['comparison']['relax_structure_indices'] == [0, 1]
        assert evidence['comparison']['sp_structure_indices'] == [0, 1, 2]
        ranking(root)
        evidence = build_evidence(root)
        assert evidence['comparison']['included_model_keys'] == ['a']
        assert evidence['comparison']['relax_structure_indices'] == [0, 1, 2]
        assert evidence['models'][0]['full_sp']['n'] == 3
        for mode in ('missing', 'failed', 'nonfinite_model', 'nonfinite_row_dft', 'missing_dft', 'nonfinite_dft'):
            root = fixture(base/mode)
            original = read_csv(root/'summary/benchmark_predictions_long.csv')
            rows = [dict(row) for row in original]
            target = next(r for r in rows if r['model_key']=='b' and r['task']=='SP' and r['structure_index']=='2')
            references = read_csv(root/'summary/dft_reference.csv')
            if mode == 'missing': rows.remove(target)
            elif mode == 'failed': target['status'] = 'failed'
            elif mode == 'nonfinite_model': target['model_adsorption_energy_eV'] = 'nan'
            elif mode == 'nonfinite_row_dft': target['dft_adsorption_energy_eV'] = 'nan'
            elif mode == 'missing_dft': csv_file(root/'summary/dft_reference.csv', references[:2])
            else:
                bad = [dict(r) for r in references]
                bad[2]['dft_adsorption_energy_eV'] = 'inf'
                csv_file(root/'summary/dft_reference.csv', bad)
            csv_file(root/'summary/benchmark_predictions_long.csv', rows)
            assert ranking(root) == 0
            audit = load_json(root/'summary/ranking_audit.json')
            assert audit['status'] == 'pending_sp_completion'
            assert audit['full_dataset_indices'] == [0, 1, 2]
            assert audit['sp_coverage']['b']['missing_or_failed_indices'] == [2]
            assert read_csv(root/'summary/model_ranking.csv') == []
            evidence, _ = write_reviewable(root)
            assert evidence['models'][1]['full_sp']['mae_eV'] is None
            assert evidence['models'][1]['coverage']['SP'] == 2/3
            assert not validate_report(root, final=True)['passed']
            csv_file(root/'summary/benchmark_predictions_long.csv', original)
            csv_file(root/'summary/dft_reference.csv', references)
            assert ranking(root) == 0
            assert not validate_report(root, final=True)['passed']  # stale report
            write_reviewable(root)
            assert validate_report(root, final=True)['passed']
        root = fixture(base/'relax_failed', bad=True)
        rows = read_csv(root/'summary/benchmark_predictions_long.csv')
        for row in rows:
            if row['model_key']=='b' and row['task']=='SP' and row['structure_index']=='2':
                row['model_adsorption_energy_eV'] = '3'
        csv_file(root/'summary/benchmark_predictions_long.csv', rows)
        ranking(root, '--reliability-threshold', '0.5')
        evidence, _ = write_reviewable(root)
        assert abs(evidence['models'][1]['full_sp']['mae_eV'] - 6.4/3) < 1e-12
        assert evidence['models'][1]['common_relax']['n'] == 2
        stale = dict(evidence, schema_version=2)
        atomic_write_json(root/'summary'/EVIDENCE, stale)
        assert not validate_report(root, final=True)['passed']
        config = load_json(root/'benchmark_config.json')
        config['structure_indices'] = [0, 1]
        atomic_write_json(root/'benchmark_config.json', config)
        assert ranking(root) == 1
    print('Full SP self-test passed (coverage, repair, cohort, numerical and stale evidence gates)')


if __name__ == '__main__':
    main()
