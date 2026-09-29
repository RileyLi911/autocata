"""Verify default/custom effective files, explicit confirmation and stale-profile gates."""
import copy
import tempfile
from pathlib import Path
from common import canonical_json_sha256, atomic_write_json, load_json, sha256_file
from dft_profile import draft, preview, confirm, payload, effective_incar, SYSTEMS
from dft_reference_control import validate_profile, write_incar, write_kpoints, DFTControlError, prepare, collect, build_submission
from dft_self_test import create_fixture_run, execution_profile
from run_control import initialize_frames


def rejected(call):
    try:
        call()
    except (ValueError, DFTControlError):
        return
    raise AssertionError('invalid or stale settings were accepted')


def resolved():
    return draft({'potcar_generation': {'backend':'direct', 'potcar_root':'/fixture/authorized',
        'dataset_version':'synthetic-test', 'element_labels':{'Cu':'Cu','N':'N','O':'O'}},
        'components':{k:{'initial_magmoms_by_element':{'Cu':0,'N':1,'O':0}} for k in SYSTEMS}}, 'synthetic current-system evidence')


def accept(p):
    return confirm(p, preview(p)['preview_sha256'])


def main():
    p = draft()
    assert not p['confirmed'] and p['incar']['GGA']=='PE' and p['incar']['EDIFF']==1e-5
    assert p['isolated_adsorbate']['vacuum_A']==10
    assert p['isolated_adsorbate']['spin_multiplicity'] is None
    rejected(lambda: validate_profile(p))
    rejected(lambda: accept(p))  # unresolved POTCAR and moments
    p = resolved()
    digest = preview(p)['preview_sha256']
    changed = copy.deepcopy(p)
    changed['incar']['ENCUT'] = 600
    rejected(lambda: confirm(changed, digest))
    p = accept(p)
    validate_profile(p)
    frozen = copy.deepcopy(p)
    validate_profile(p)
    assert p == frozen  # resume neither asks nor modifies confirmation
    changed = copy.deepcopy(p)
    changed['incar']['ENCUT'] = 600
    rejected(lambda: validate_profile(changed))
    custom = resolved()
    custom['incar'].update(ENCUT=600, EDIFFG=-0.03, NSW=100)
    custom['relaxation'].update(force_threshold_eV_per_A=0.03, max_ionic_steps=100)
    custom['kpoints']['grid'] = [3,3,1]
    custom = accept(custom)
    assert effective_incar(custom, 'clean_slab', ['Cu'])['NSW']==100
    for tag, value in [('NSW', 42), ('EDIFFG', -.1), ('ISIF', 3), ('ISPIN', 1)]:
        bad = resolved()
        bad['incar'][tag] = value
        rejected(lambda: accept(bad))
    charged = resolved()
    charged['components']['isolated_adsorbate']['charge'] = 1
    charged['isolated_adsorbate']['charge'] = 1
    rejected(lambda: accept(charged))
    charged['potcar_generation'].update(valence_electrons_by_element={'N':5,'O':6,'Cu':11}, valence_source='synthetic verified ZVAL')
    charged['isolated_adsorbate']['spin_multiplicity'] = 2
    charged = accept(charged)
    actual = effective_incar(charged, 'isolated_adsorbate', ['N','O','O','O'])
    assert actual['NELECT']==22 and actual['NUPDOWN']==1
    site = resolved()
    site['components']['clean_slab'].update(initial_magmoms_by_element=None, initial_magmoms=[1,-1])
    site = accept(site)
    assert effective_incar(site, 'clean_slab', ['Cu','Cu'])['MAGMOM'] == [1,-1]
    rejected(lambda: effective_incar(site, 'clean_slab', ['Cu']))
    component = resolved()
    component['components']['isolated_adsorbate']['incar'] = {'NELM': 42}
    component = accept(component)
    assert effective_incar(component, 'isolated_adsorbate')['NELM'] == 42
    with tempfile.TemporaryDirectory(prefix='catqc-dft-defaults-') as directory:
        base = Path(directory)
        for system in SYSTEMS:
            write_incar(base/'INCAR', effective_incar(p, system, ['Cu','N','O']))
            write_kpoints(base/'KPOINTS', p['kpoints'], system)
            text = (base/'INCAR').read_text()
            assert 'GGA = PE' in text and 'EDIFF = 1e-05' in text and 'NSW = 500' in text
            assert 'EDIFFG = -0.05' in text and 'MAGMOM = 0 1 0' in text
            assert 'NUPDOWN' not in text and 'NELECT' not in text
            assert ('1 1 1' if system=='isolated_adsorbate' else '2 2 1') in (base/'KPOINTS').read_text()
        # Exercise actual ASE preparation, rather than only serializing dictionaries.
        from ase import Atoms
        from ase.io import write, read
        root = create_fixture_run(base/'work', Path(__file__).resolve().parents[1], 'generate_vasp', 'fixture')
        initialize_frames(root, 'fixture', 1, 'NO3')
        atoms = Atoms('Cu4NO3', positions=[(1,1,1),(3,1,1),(1,3,1),(3,3,1),(2,2,3),(2.8,2,3),(1.6,2.7,3),(1.6,1.3,3)], cell=[8,8,12], pbc=True)
        write(base/'input.extxyz', atoms)
        atomic_write_json(root/'benchmark_config.json', {'frame_count':1, 'structure_indices':[0], 'structure_path':str(base/'input.extxyz'),
            'benchmark_request':{'dft_reference_mode':'generate_vasp'}, 'dft_reference':{'structure_derivation':{'adsorbate_indices':[4,5,6,7]}}})
        atomic_write_json(base/'execution.json', execution_profile())
        atomic_write_json(base/'profile.json', resolved())
        rejected(lambda: prepare(root,'fixture',base/'profile.json',base/'execution.json'))
        assert not (root/'dft/dft_manifest.json').exists()
        atomic_write_json(base/'profile.json', p)
        manifest = prepare(root,'fixture',base/'profile.json',base/'execution.json')
        assert len(manifest['calculations'])==3
        for item in manifest['calculations']:
            checkpoint = load_json(root/item['checkpoint_path'])
            inputs = (root/checkpoint['input_manifest_path']).parent/'inputs'
            assert 'EDIFF = 1e-05' in (inputs/'INCAR').read_text()
            system = item['system_type']
            assert ('1 1 1' if system=='isolated_adsorbate' else '2 2 1') in (inputs/'KPOINTS').read_text()
            if system=='isolated_adsorbate':
                molecule = read(inputs/'POSCAR')
                for axis in range(3):
                    assert abs(min(molecule.positions[:,axis])-10)<1e-10
                    assert abs(molecule.cell[axis,axis]-max(molecule.positions[:,axis])-10)<1e-10
        atomic_write_json(root/'dft/execution_preflight.json', {'status':'passed',
            'execution_profile_sha256':sha256_file(root/'dft/execution_profile.json')})
        build_submission(root,'fixture')
        input_file = inputs/'INCAR'
        saved = input_file.read_bytes()
        input_file.write_bytes(saved+b'ENCUT = 700\n')
        rejected(lambda: build_submission(root,'fixture'))
        input_file.write_bytes(saved)
        atomic_write_json(root/'dft/vasp_profile.json', custom)
        rejected(lambda: collect(root,'fixture'))
        rejected(lambda: build_submission(root,'fixture'))
    print('DFT defaults self-test passed (actual ASE files, preview, customization, charge/spin, resume and invalidation)')


if __name__ == '__main__':
    main()
