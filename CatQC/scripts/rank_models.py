"""Reliability-filtered weighted model ranking; standard library only."""
from __future__ import annotations
import argparse
import csv
import json
import math
import sys
from pathlib import Path
from common import atomic_write_json, as_bool, as_float, load_json, metric_values, read_csv, sha256_file

CATEGORIES = ('Invalid', 'Unconverged', 'Dissociated', 'Detached', 'Intact and converged')
STATUS_OK = {'success', 'ok', 'completed', 'complete', 'valid', 'done'}
DEFAULT_WEIGHTS = {'relax_mae': 5.0, 'sp_mae': 3.0, 'spearman': 2.0}
METRICS = {'relax_mae': 'relax_mae_eV', 'sp_mae': 'sp_mae_eV', 'spearman': 'rho_sp'}
SOURCES = ('benchmark_config.json', 'models_manifest.json', 'summary/benchmark_predictions_long.csv', 'summary/ranking_config.json', 'summary/dft_reference.csv')
RELIABILITY_COLUMNS = ['model_key', 'model_name', 'n_relax_expected', 'n_intact_converged', 'r_relax', 'invalid_count', 'unconverged_count', 'dissociated_count', 'detached_count', 'missing_count', 'unclassified_count', 'reliability_review_status', 'review_reason']
RANKING_COLUMNS = ['model_key', 'model_name', 'n_sp', 'sp_mae_eV', 'rho_sp', 'n_relax_common', 'relax_mae_eV', 'n_relax_expected', 'n_intact_converged', 'r_relax', *['score_' + k for k in METRICS], *['contribution_' + k for k in METRICS], 'total_score', 'rank']


def write_csv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def normalize_weights(weights):
    if not isinstance(weights, dict) or set(weights) != set(DEFAULT_WEIGHTS):
        raise ValueError('weights must contain relax_mae, sp_mae and spearman')
    values = {k: as_float(weights[k]) for k in DEFAULT_WEIGHTS}
    if any(isinstance(weights[k], bool) or v is None or v < 0 for k, v in values.items()):
        raise ValueError('weights must be finite nonnegative numbers')
    scale = max(values.values())
    if scale == 0:
        raise ValueError('weight sum must be positive')
    scaled = {k: v / scale for k, v in values.items()}
    return {k: v / sum(scaled.values()) for k, v in scaled.items()}


def validate_settings(settings):
    threshold = as_float(settings.get('reliability_threshold'))
    if isinstance(settings.get('reliability_threshold'), bool) or threshold is None or not 0 <= threshold <= 1:
        raise ValueError('reliability_threshold must be finite and in [0, 1]')
    if settings.get('normalized_weights') != normalize_weights(settings.get('raw_weights')):
        raise ValueError('normalized_weights do not match raw_weights')
    if settings.get('schema_version') != 1 or not isinstance(settings.get('selection_source'), dict):
        raise ValueError('ranking settings need schema_version=1 and selection_source')
    for field in ('reliability_threshold', 'weights'):
        if not isinstance(settings['selection_source'].get(field), str) or not settings['selection_source'][field].strip():
            raise ValueError('selection_source must record both user choices')
    return settings


def configure(path, args):
    existing = validate_settings(load_json(path)) if path.exists() else None
    supplied = args.accept_defaults or args.reliability_threshold is not None or args.weights is not None
    if not supplied:
        return existing
    threshold = 0.95 if args.accept_defaults or existing is None else existing['reliability_threshold']
    raw = dict(DEFAULT_WEIGHTS if args.accept_defaults or existing is None else existing['raw_weights'])
    origin = dict(existing['selection_source'] if existing and not args.accept_defaults else {})
    source = args.selection_source or 'explicit CLI choice'
    if args.reliability_threshold is not None:
        threshold = args.reliability_threshold
    if args.weights is not None:
        raw = dict(zip(DEFAULT_WEIGHTS, args.weights))
    for key, custom in (('reliability_threshold', args.reliability_threshold is not None), ('weights', args.weights is not None)):
        if custom or args.accept_defaults or existing is None:
            origin[key] = source + (' (custom)' if custom else ' (accepted default)')
    settings = validate_settings({'schema_version': 1, 'reliability_threshold': threshold, 'raw_weights': raw,
        'normalized_weights': normalize_weights(raw), 'selection_source': origin})
    if settings != existing:
        atomic_write_json(path, settings)
    return settings


def fixed_ok(row):
    indices = row.get('fixed_atom_indices')
    if isinstance(indices, str):
        try:
            indices = json.loads(indices)
        except ValueError:
            return False
    maximum = as_float(row.get('fixed_atom_max_displacement_A'))
    tolerance = as_float(row.get('fixed_atom_displacement_tolerance_A'))
    return (isinstance(indices, list) and bool(indices) and maximum is not None and tolerance is not None
        and 0 <= maximum <= tolerance and math.isclose(tolerance, 0.05, abs_tol=1e-12)
        and as_bool(row.get('fixed_atom_displacement_violation')) is False)


def score_metrics(metrics, weights):
    normalized = normalize_weights(weights)
    rows = [dict(row) for row in metrics]
    if not rows:
        return []
    for key, field in METRICS.items():
        values = [as_float(row.get(field)) for row in rows]
        if any(value is None for value in values):
            raise ValueError(f'cannot rank: missing or undefined {field}')
        low, high = min(values), max(values)
        for row, value in zip(rows, values):
            score = 1.0 if high == low else ((value - low) if key == 'spearman' else (high - value)) / (high - low)
            row['score_' + key] = score
            row['contribution_' + key] = score * normalized[key]
    for row in rows:
        row['total_score'] = sum(row['contribution_' + k] for k in METRICS)
    rows.sort(key=lambda row: (-row['total_score'], row['model_name'], row['model_key']))
    # Anchor ties at the group's highest score; avoid chained near-ties.
    start = 0
    while start < len(rows):
        end = start + 1
        while end < len(rows) and rows[start]['total_score'] - rows[end]['total_score'] <= 1e-12:
            end += 1
        for row in rows[start:end]:
            row['rank'] = start + 1
        rows[start:end] = sorted(rows[start:end], key=lambda row: (row['model_name'], row['model_key']))
        start = end
    return rows


def full_indices(config):
    """Frozen input ordering is the complete positional index list, never predictions."""
    expected = int(config['frame_count'])
    indices = config.get('structure_indices')
    if indices != list(range(expected)) or any(type(i) is not int for i in indices):
        raise ValueError('structure_indices must match the complete frozen input ordering')
    return indices


def sp_coverage(root, config, active, by_model):
    indices = full_indices(config)
    references = {}
    for row in read_csv(root / 'summary/dft_reference.csv'):
        index = int(row['structure_index'])
        if index in references or index not in indices:
            raise ValueError('duplicate or out-of-range DFT reference index')
        references[index] = as_float(row.get('dft_adsorption_energy_eV'))
    coverage = {}
    for key in active:
        problems = []
        for index in indices:
            row = by_model[key].get(index)
            reasons = []
            reference = references.get(index)
            if reference is None:
                reasons.append('missing or nonfinite DFT reference')
            if row is None:
                reasons.append('missing SP prediction')
            else:
                if row.get('status', '').lower() not in STATUS_OK:
                    reasons.append('failed SP status: ' + row.get('status', ''))
                if as_float(row.get('model_adsorption_energy_eV')) is None:
                    reasons.append('nonfinite SP prediction')
                value = as_float(row.get('dft_adsorption_energy_eV'))
                if value is None:
                    reasons.append('missing or nonfinite SP DFT reference')
                elif reference is not None and value != reference:
                    raise ValueError(f'inconsistent DFT reference for SP/{key}/{index}')
            if reasons:
                problems.append({'structure_index': index, 'reasons': reasons})
        coverage[key] = {'expected': len(indices), 'valid': len(indices)-len(problems),
                         'fraction': (len(indices)-len(problems))/len(indices),
                         'missing_or_failed_indices': [r['structure_index'] for r in problems],
                         'problems': problems}
    return coverage


def evaluate(root, settings):
    """Recompute evidence without writing; also used by the report audit gate."""
    validate_settings(settings)
    config = load_json(root / 'benchmark_config.json')
    identities = load_json(root / 'models_manifest.json')['models']
    expected = int(config['frame_count'])
    tasks = config['benchmark_request']['requested_tasks']
    if expected < 1 or not tasks or set(tasks) - {'single_point', 'relaxation'}:
        raise ValueError('invalid expected frame count or requested tasks')
    keys = [m['model_key'] for m in identities]
    if not keys or len(keys) != len(set(keys)):
        raise ValueError('manifest must contain unique model keys')
    by_task = {task: {key: {} for key in keys} for task in ('SP', 'Relax')}
    for row in read_csv(root / 'summary/benchmark_predictions_long.csv'):
        key, task, index = row['model_key'], row['task'], int(row['structure_index'])
        if key not in keys or task not in by_task or not 0 <= index < expected:
            raise ValueError('unknown model/task or out-of-range structure index')
        if {'SP': 'single_point', 'Relax': 'relaxation'}[task] not in tasks:
            raise ValueError('prediction row belongs to an unrequested task')
        if index in by_task[task][key]:
            raise ValueError(f'duplicate prediction row: {key}/{task}/{index}')
        by_task[task][key][index] = row
    valid = {task: {key: {} for key in keys} for task in by_task}
    for task, models in by_task.items():
        for key, frames in models.items():
            for index, row in frames.items():
                usable = (row.get('status', '').lower() in STATUS_OK and as_float(row.get('model_adsorption_energy_eV')) is not None and as_float(row.get('dft_adsorption_energy_eV')) is not None)
                if task == 'Relax':
                    usable = usable and as_bool(row.get('converged')) is True and fixed_ok(row)
                    if row.get('reliability_category') == CATEGORIES[-1] and not usable:
                        raise ValueError(f'reliable Relax row lacks valid energy, convergence, or fixed-layer evidence: {key}/{index}')
                if usable:
                    valid[task][key][index] = row
    review = []
    for model in identities:
        key = model['model_key']
        frames = by_task['Relax'][key]
        counts = {c: sum(row.get('reliability_category') == c for row in frames.values()) for c in CATEGORIES}
        rate = counts[CATEGORIES[-1]] / expected if 'relaxation' in tasks else None
        state, reason = 'pass', 'meets reliability threshold'
        if not model.get('enabled', True):
            state, reason = 'disabled', 'disabled in model manifest'
        elif rate is None:
            state, reason = 'not_applicable', 'Relax was not requested'
        elif rate < settings['reliability_threshold']:
            state, reason = 'excluded', 'below reliability threshold'
        review.append({'model_key': key, 'model_name': model.get('model_name', key), 'n_relax_expected': expected if rate is not None else None,
            'n_intact_converged': counts[CATEGORIES[-1]], 'r_relax': rate,
            **{name + '_count': counts[c] for name, c in zip(('invalid', 'unconverged', 'dissociated', 'detached'), CATEGORIES)},
            'missing_count': expected - len(frames) if rate is not None else None, 'unclassified_count': len(frames) - sum(counts.values()),
            'reliability_review_status': state, 'review_reason': reason})
    active = [r['model_key'] for r in review if r['reliability_review_status'] not in {'excluded', 'disabled'}]
    indices = full_indices(config)
    coverage = sp_coverage(root, config, active, by_task['SP'])
    incomplete = any(item['problems'] for item in coverage.values())
    common = {}
    for task in by_task:
        subsets = [{i for i, row in valid[task][key].items() if task == 'SP' or row.get('reliability_category') == CATEGORIES[-1]} for key in active]
        common[task] = sorted(set.intersection(*subsets)) if subsets else []
        if task == 'SP':
            common[task] = indices if 'single_point' in tasks else []
        for index in common[task]:
            if not active or (task == 'SP' and incomplete):
                continue
            references = {float(valid[task][key][index]['dft_adsorption_energy_eV']) for key in active}
            if len(references) != 1:
                raise ValueError(f'inconsistent DFT reference for {task}/{index}')
    reason = None
    if set(tasks) != {'single_point', 'relaxation'}:
        reason = 'Three-metric ranking requires both SP and Relax tasks.'
    elif not active:
        reason = 'No models remain after reliability filtering.'
    elif incomplete:
        reason = 'Full-dataset SP coverage is incomplete; complete every included model before ranking.'
    elif not common['SP'] or not common['Relax']:
        reason = 'Full SP or common intact-converged Relax sample set is empty.'
    metrics = []
    if reason is None:
        for item in review:
            key = item['model_key']
            if key not in active:
                continue
            values = {}
            for task in common:
                sample = [valid[task][key][i] for i in common[task]]
                values[task] = metric_values([float(r['model_adsorption_energy_eV']) for r in sample], [float(r['dft_adsorption_energy_eV']) for r in sample])
            metrics.append({**item, 'n_sp': len(common['SP']), 'n_relax_common': len(common['Relax']),
                'sp_mae_eV': values['SP']['mae_eV'], 'rho_sp': values['SP']['spearman'], 'relax_mae_eV': values['Relax']['mae_eV']})
        if any(as_float(row[field]) is None for row in metrics for field in METRICS.values()):
            reason = 'A required metric is undefined (including Spearman for constant values or fewer than two SP samples).'
    ranking = score_metrics(metrics, settings['raw_weights']) if reason is None else []
    audit = {'schema_version': 2, 'status': ('complete' if reason is None else 'pending_sp_completion' if incomplete and active and set(tasks) == {'single_point', 'relaxation'} else 'unavailable'), 'reason': reason,
        'model_keys_original': keys, 'model_keys_included': active,
        'model_keys_excluded': [r['model_key'] for r in review if r['reliability_review_status'] == 'excluded'],
        'model_keys_disabled': [r['model_key'] for r in review if r['reliability_review_status'] == 'disabled'],
        'sample_policy': {'SP': 'full_sp', 'Relax': 'common_relax'},
        'full_dataset_indices': indices, 'full_dataset_size': len(indices), 'sp_coverage': coverage,
        'sp_structure_indices': common['SP'], 'relax_structure_indices': common['Relax'],
        'n_sp': len(common['SP']), 'n_relax_common': len(common['Relax']), 'settings': settings,
        'source_sha256': {name: sha256_file(root / name) for name in SOURCES}}
    return review, ranking, audit


def run(root, args):
    summary = root / 'summary'
    settings = configure(summary / 'ranking_config.json', args)
    if settings is None:
        print('Ask the user together: accept reliability >= 0.95 and Relax MAE:SP MAE:Spearman = 5:3:2, or customize? Then pass --accept-defaults or explicit values; saved choices are reused.')
        return 2
    review, ranking, audit = evaluate(root, settings)
    write_csv(summary / 'model_reliability_review.csv', RELIABILITY_COLUMNS, review)
    # Replace earlier rankings with an empty headed table when unavailable.
    write_csv(summary / 'model_ranking.csv', RANKING_COLUMNS, ranking)
    method = ('Reliability-filtered weighted model ranking\n'
        'R_relax = Intact and converged / full expected Relax frame count; below threshold is excluded.\n'
        f"Threshold: {settings['reliability_threshold']}; normalized weights: {settings['normalized_weights']}.\n"
        f"Full-dataset SP N={audit['n_sp']}; common intact-converged Relax K={audit['n_relax_common']}.\n"
        'MAE score=(max-x)/(max-min); SP Spearman score=(x-min)/(max-min), across included models.\n'
        'Constant metric scores equal 1. Weighted sum is sorted descending. Ties within 1e-12 of group maximum share competition rank; names order display only.\n'
        'Normalization depends on the included cohort; scores are not absolute accuracy or statistical significance.\n'
        f"Status: {audit['status']}; reason: {audit['reason']}.\n")
    (summary / 'methodology.txt').write_text(method, encoding='utf-8')
    audit['output_sha256'] = {name: sha256_file(summary / name) for name in ('model_reliability_review.csv', 'model_ranking.csv', 'methodology.txt')}
    atomic_write_json(summary / 'ranking_audit.json', audit)
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('benchmark_root', type=Path)
    parser.add_argument('--accept-defaults', action='store_true', help="Record user's explicit acceptance of defaults")
    parser.add_argument('--reliability-threshold', type=float)
    parser.add_argument('--weights', type=float, nargs=3, metavar=('RELAX_MAE', 'SP_MAE', 'SPEARMAN'))
    parser.add_argument('--selection-source', help='User message/choice provenance; omitted on resume')
    args = parser.parse_args()
    try:
        return run(args.benchmark_root.resolve(), args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
