"""Observable ranking regression cases, using synthetic local data only."""
from __future__ import annotations
import contextlib
import io
import math
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from common import atomic_write_json, load_json, read_csv
from model_selection_report_self_test import fixture, csv_file
from rank_models import main as ranking_main, score_metrics, normalize_weights, DEFAULT_WEIGHTS


def invoke(root, *extra):
    with patch.object(sys, 'argv', ['rank_models.py', str(root), *extra]), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return ranking_main()


def main():
    checks = 0
    # Independent hand-calculated expected values: A=.7, B=.3, C=.5.
    metrics = [dict(model_key=k, model_name=k.upper(), relax_mae_eV=r, sp_mae_eV=s, rho_sp=rho)
               for k, r, s, rho in [('a', 1, 3, 1), ('b', 3, 1, -1), ('c', 2, 2, 0)]]
    result = score_metrics(metrics, DEFAULT_WEIGHTS)
    assert [r['model_key'] for r in result] == ['a', 'c', 'b']
    for row, score in zip(result, [.7, .5, .3]):
        assert math.isclose(row['total_score'], score, abs_tol=1e-12)
    assert result[0]['contribution_relax_mae'] == .5
    assert result[0]['contribution_sp_mae'] == 0
    assert result[0]['contribution_spearman'] == .2
    assert [r['rank'] for r in result] == [1, 2, 3]
    assert normalize_weights(dict(relax_mae=50, sp_mae=30, spearman=20)) == normalize_weights(DEFAULT_WEIGHTS)
    assert normalize_weights(dict(relax_mae=1e308, sp_mae=1e308, spearman=1e308)) == dict.fromkeys(DEFAULT_WEIGHTS, 1/3)
    checks += 1
    constant = [dict(row, relax_mae_eV=1, sp_mae_eV=1, rho_sp=-1) for row in metrics]
    tied = score_metrics(constant, DEFAULT_WEIGHTS)
    assert all(row['rank'] == 1 and row['total_score'] == 1 for row in tied)
    assert score_metrics([metrics[0]], DEFAULT_WEIGHTS)[0]['total_score'] == 1
    # Near ties are anchored at the maximum and displayed alphabetically.
    near = [dict(model_key=k, model_name=k, relax_mae_eV=0, sp_mae_eV=0, rho_sp=v)
            for k, v in [('z', 1), ('a', 1 - 5e-13), ('b', 1 - 2e-12), ('c', 0)]]
    near = score_metrics(near, dict(relax_mae=0, sp_mae=0, spearman=1))
    assert [(r['model_key'], r['rank']) for r in near] == [('a', 1), ('z', 1), ('b', 3), ('c', 4)]
    checks += 1
    with tempfile.TemporaryDirectory(prefix='weighted-ranking-') as directory:
        base = Path(directory)
        root = fixture(base / 'default', bad=True)
        assert invoke(root) == 2
        assert not (root / 'summary/ranking_config.json').exists()
        assert invoke(root, '--accept-defaults', '--selection-source', 'fixture user choice') == 0
        settings = (root / 'summary/ranking_config.json').read_bytes()
        ranking = (root / 'summary/model_ranking.csv').read_bytes()
        assert load_json(root / 'summary/ranking_config.json')['normalized_weights'] == dict(relax_mae=.5, sp_mae=.3, spearman=.2)
        assert [r['model_key'] for r in read_csv(root / 'summary/model_ranking.csv')] == ['a']
        assert read_csv(root / 'summary/model_reliability_review.csv')[1]['reliability_review_status'] == 'excluded'
        assert invoke(root) == 0
        assert (root / 'summary/ranking_config.json').read_bytes() == settings
        assert (root / 'summary/model_ranking.csv').read_bytes() == ranking
        checks += 1
        assert invoke(root, '--reliability-threshold', str(2/3), '--weights', '6', '3', '1') == 0
        assert len(read_csv(root / 'summary/model_ranking.csv')) == 2
        cfg = load_json(root / 'summary/ranking_config.json')
        assert math.isclose(cfg['normalized_weights']['relax_mae'], .6)
        assert invoke(root, '--reliability-threshold', str(2/3 + 1e-9)) == 0
        assert len(read_csv(root / 'summary/model_ranking.csv')) == 1
        assert load_json(root / 'summary/ranking_config.json')['raw_weights'] == cfg['raw_weights']
        checks += 1
        for flags in [('--reliability-threshold', '-.1'), ('--reliability-threshold', '1.1'), ('--reliability-threshold', 'nan'),
                      ('--weights', '0', '0', '0'), ('--weights', '-1', '3', '2'), ('--weights', 'inf', '3', '2')]:
            saved = (root / 'summary/ranking_config.json').read_bytes()
            assert invoke(root, *flags) == 1
            assert (root / 'summary/ranking_config.json').read_bytes() == saved
        for value in ('0', '1'):
            assert invoke(root, '--reliability-threshold', value) == 0
        checks += 1
        # All excluded and missing rows remain in the denominator.
        rows = read_csv(root / 'summary/benchmark_predictions_long.csv')
        rows = [r for r in rows if not (r['task'] == 'Relax' and r['structure_index'] == '0')]
        csv_file(root / 'summary/benchmark_predictions_long.csv', rows)
        assert invoke(root, '--accept-defaults') == 0
        assert load_json(root / 'summary/ranking_audit.json')['status'] == 'unavailable'
        assert read_csv(root / 'summary/model_ranking.csv') == []
        assert all(r['missing_count'] == '1' for r in read_csv(root / 'summary/model_reliability_review.csv'))
        checks += 1
        for name, options in [('constant', {'constant': True}), ('disjoint', {'disjoint': True}),
                              ('sp_only', {'tasks': ('single_point',)}), ('relax_only', {'tasks': ('relaxation',)})]:
            edge = fixture(base / name, **options)
            assert invoke(edge, '--accept-defaults') == 0
            audit = load_json(edge / 'summary/ranking_audit.json')
            assert audit['status'] == ('pending_sp_completion' if name == 'disjoint' else 'unavailable') and audit['reason']
            assert read_csv(edge / 'summary/model_ranking.csv') == []
            checks += 1
        # A zero correlation weight never licenses filling undefined correlation.
        edge = base / 'constant'
        assert invoke(edge, '--weights', '1', '1', '0') == 0
        assert load_json(edge / 'summary/ranking_audit.json')['status'] == 'unavailable'
        checks += 1
        # Empty Relax intersection and single common SP sample.
        for mode in ('relax_disjoint', 'one_sp'):
            edge = fixture(base / mode)
            rows = read_csv(edge / 'summary/benchmark_predictions_long.csv')
            if mode == 'one_sp':
                rows = [r for r in rows if r['task'] != 'SP' or r['structure_index'] == '0']
            else:
                for row in rows:
                    if row['task'] == 'Relax' and ((row['model_key'] == 'a') != (row['structure_index'] == '0')):
                        row['reliability_category'] = 'Detached'
            csv_file(edge / 'summary/benchmark_predictions_long.csv', rows)
            assert invoke(edge, '--reliability-threshold', '0') == 0
            assert load_json(edge / 'summary/ranking_audit.json')['status'] == ('pending_sp_completion' if mode == 'one_sp' else 'unavailable')
            checks += 1
        # Malformed reliable geometry, duplicates, and inconsistent DFT references are hard failures.
        for mode in ('fixed', 'duplicate', 'reference'):
            edge = fixture(base / mode)
            rows = read_csv(edge / 'summary/benchmark_predictions_long.csv')
            if mode == 'fixed': rows[-1]['fixed_atom_max_displacement_A'] = '.1'
            elif mode == 'duplicate': rows.append(dict(rows[0]))
            else: rows[6]['dft_adsorption_energy_eV'] = '999'
            csv_file(edge / 'summary/benchmark_predictions_long.csv', rows)
            assert invoke(edge, '--accept-defaults') == 1
            checks += 1
        # Disabled models are visible but cannot change cohort normalization.
        edge = fixture(base / 'disabled')
        manifest = load_json(edge / 'models_manifest.json')
        manifest['models'][1]['enabled'] = False
        atomic_write_json(edge / 'models_manifest.json', manifest)
        assert invoke(edge, '--accept-defaults') == 0
        assert len(read_csv(edge / 'summary/model_ranking.csv')) == 1
        assert read_csv(edge / 'summary/model_reliability_review.csv')[1]['reliability_review_status'] == 'disabled'
        checks += 1
    print(f'Weighted ranking self-test passed ({checks} scenario groups)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
