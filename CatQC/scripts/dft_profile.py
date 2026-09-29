"""Preview and freeze user-selected VASP settings; never submit calculations."""
from __future__ import annotations
import argparse
import copy
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from common import atomic_write_json, canonical_json_sha256, load_json

SYSTEMS = ('slab_adsorbate', 'clean_slab', 'isolated_adsorbate')
TEMPLATE = Path(__file__).resolve().parents[1] / 'assets/vasp_defaults.json'


def merge(base, overrides):
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


def payload(profile):
    return {k: v for k, v in profile.items() if k not in {'confirmed', 'confirmed_by', 'confirmed_at', 'confirmation_sha256'}}


def draft(overrides=None, source='user-provided overrides'):
    defaults = load_json(TEMPLATE)
    profile = merge(defaults, overrides or {})
    profile.update(confirmed=False, confirmed_by=None, confirmed_at=None)
    profile.pop('confirmation_sha256', None)
    profile['selection_provenance'] = {'template_id': defaults['profile_id'],
        'defaults': defaults, 'overrides': overrides or {}, 'override_source': source}
    return profile


def finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number')
    return value


def effective_incar(profile, system, symbols=None):
    """Resolve metadata into executable tags; reject contradictory duplicate settings."""
    component = profile['components'][system]
    incar = merge(profile['incar'], component.get('incar', {}))
    if any(key != key.upper() for key in incar):
        raise ValueError('INCAR keys must use uppercase spelling')
    def assign(key, value):
        if key in incar and incar[key] != value:
            raise ValueError(f'{system}: {key} conflicts with effective component metadata')
        incar[key] = value
    relaxation = profile['relaxation']
    force = finite(relaxation['force_threshold_eV_per_A'], 'force threshold')
    steps = relaxation['max_ionic_steps']
    if force <= 0 or type(steps) is not int or steps < 1:
        raise ValueError('force threshold and integer ionic step limit must be positive')
    assign('ISIF', 2)
    assign('EDIFFG', -force)
    assign('NSW', steps)
    if incar.get('IBRION') not in {1, 2, 3}:
        raise ValueError('DFT reference requires an ionic relaxation IBRION')
    spin = component['ispin']
    if type(spin) is not int or spin not in {1, 2}:
        raise ValueError('component ispin must be 1 or 2')
    assign('ISPIN', spin)
    moments = component.get('initial_magmoms_by_element')
    atom_moments = component.get('initial_magmoms')
    if spin == 2:
        if incar.get('LNONCOLLINEAR') or incar.get('LSORBIT'):
            raise ValueError('this collinear profile requires a separate protocol for noncollinear/SOC settings')
        if atom_moments is not None:
            if not isinstance(atom_moments, list) or not atom_moments:
                raise ValueError('initial_magmoms must be a nonempty atom-order array')
            if moments:
                raise ValueError('choose either atom-order or per-element initial moments')
            for value in atom_moments:
                finite(value, 'initial moment')
            if symbols is not None and len(atom_moments) != len(symbols):
                raise ValueError('initial_magmoms length must match the actual component atom count')
            assign('MAGMOM', atom_moments)
        else:
            if not isinstance(moments, dict) or not moments:
                raise ValueError(f'{system}: initial magnetic moments must be resolved and shown to the user')
            for element, value in moments.items():
                finite(value, 'initial moment for ' + element)
            if symbols is not None:
                try:
                    assign('MAGMOM', [moments[element] for element in symbols])
                except KeyError as exc:
                    raise ValueError(f'{system}: missing initial magnetic moment for {exc}') from exc
    elif 'MAGMOM' in incar or atom_moments or moments:
        raise ValueError('initial magnetic moments conflict with ISPIN=1')
    charge = finite(component['charge'], 'component charge')
    if system == 'isolated_adsorbate':
        if charge != profile['isolated_adsorbate']['charge']:
            raise ValueError('isolated charge metadata conflicts with component charge')
        multiplicity = profile['isolated_adsorbate']['spin_multiplicity']
        if multiplicity is not None:
            if type(multiplicity) is not int or multiplicity < 1 or spin != 2:
                raise ValueError('explicit spin multiplicity requires ISPIN=2 and a positive integer')
            assign('NUPDOWN', multiplicity - 1)
    if charge != 0 or 'NELECT' in incar:
        valence = profile['potcar_generation'].get('valence_electrons_by_element')
        if not valence or not profile['potcar_generation'].get('valence_source'):
            raise ValueError('charged/explicit NELECT settings require verified POTCAR ZVAL values and source')
        for element, value in valence.items():
            if finite(value, 'ZVAL for ' + element) <= 0:
                raise ValueError('ZVAL must be positive')
        if symbols is not None:
            try:
                assign('NELECT', sum(valence[element] for element in symbols) - charge)
            except KeyError as exc:
                raise ValueError(f'missing POTCAR ZVAL for {exc}') from exc
            if incar['NELECT'] <= 0:
                raise ValueError('NELECT must be positive')
    return incar


def validate_selection(profile):
    if set(profile.get('components', {})) != set(SYSTEMS):
        raise ValueError('DFT profile must resolve all three energy components')
    if not profile['potcar_generation'].get('dataset_version'):
        raise ValueError('resolve the actual POTCAR dataset version')
    if not profile.get('confirmed_at') or not isinstance(profile.get('selection_provenance'), dict):
        raise ValueError('confirmation time and selection provenance are required')
    kpoints = profile['kpoints']
    if kpoints.get('mode') == 'explicit_grid':
        grid, shift = kpoints.get('grid'), kpoints.get('shift', [0, 0, 0])
        if not isinstance(grid, list) or len(grid) != 3 or any(type(n) is not int or n < 1 for n in grid):
            raise ValueError('k-point grid must contain three positive integers')
        if not isinstance(shift, list) or len(shift) != 3:
            raise ValueError('k-point shift must contain three numbers')
        for value in shift:
            finite(value, 'k-point shift')
    elif kpoints.get('mode') == 'literal':
        if not kpoints.get('literal_lines'):
            raise ValueError('literal k-points require explicit lines')
    elif kpoints.get('mode') != 'gamma_only':
        raise ValueError('unknown k-point mode')
    for system in SYSTEMS:
        effective_incar(profile, system)
    if profile.get('confirmation_sha256') != canonical_json_sha256(payload(profile)):
        raise ValueError('DFT settings changed or are unconfirmed; preview and obtain a new user confirmation')


def preview(profile):
    components = {}
    for system in SYSTEMS:
        try:
            incar = effective_incar(profile, system)
            unresolved = None
        except (ValueError, KeyError) as exc:
            incar = merge(profile['incar'], profile.get('components', {}).get(system, {}).get('incar', {}))
            unresolved = str(exc)
        components[system] = {'incar': incar,
            'kpoints': {'mode': 'gamma_only', 'grid': [1, 1, 1], 'shift': [0, 0, 0]} if system == 'isolated_adsorbate' else profile['kpoints'],
            'component_settings': profile.get('components', {}).get(system), 'unresolved': unresolved}
    return {'preview_sha256': canonical_json_sha256(payload(profile)), 'profile': profile,
            'effective_components': components,
            'note': 'MAGMOM follows atom order using the displayed element scheme. Charged NELECT is sum(POTCAR ZVAL) minus charge. Confirm only after resolving the actual system and execution environment.'}


def confirm(profile, expected_digest):
    result = copy.deepcopy(profile)
    if canonical_json_sha256(payload(result)) != expected_digest:
        raise ValueError('preview changed; show the final settings again')
    result.update(confirmed=True, confirmed_by='user', confirmed_at=datetime.now(timezone.utc).isoformat(), confirmation_sha256=expected_digest)
    from dft_reference_control import validate_profile
    validate_profile(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['draft', 'preview', 'confirm'])
    parser.add_argument('profile', type=Path)
    parser.add_argument('--overrides', type=Path)
    parser.add_argument('--selection-source', default='user-provided overrides')
    parser.add_argument('--user-confirmed-preview-sha256', help='Only after explicit user acceptance of this preview')
    args = parser.parse_args()
    if args.command == 'draft':
        if args.profile.exists():
            raise ValueError('draft destination exists; preserve previous confirmed profiles')
        profile = draft(load_json(args.overrides) if args.overrides else {}, args.selection_source)
        atomic_write_json(args.profile, profile)
    else:
        profile = load_json(args.profile)
    if args.command == 'confirm':
        profile = confirm(profile, args.user_confirmed_preview_sha256)
        atomic_write_json(args.profile, profile)
    print(json.dumps(preview(profile), indent=2))


if __name__ == '__main__':
    main()
