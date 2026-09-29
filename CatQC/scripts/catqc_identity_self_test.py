"""CatQC identity, path and English-content acceptance checks."""
from pathlib import Path
import tempfile
from common import atomic_write_json, load_json
from self_test import new_run, selection, expect_error
from run_control import load_core, scan_matching_runs, acquire_lease, heartbeat, ControlError
from validate_run import validate
from validate_skill import validate as validate_package


def main():
    skill = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='catqc-identity-') as folder:
        base = Path(folder)
        root = new_run(base, skill, selection(1))
        manifest = load_json(root / 'run_manifest.json')
        assert manifest['skill_name'] == 'catqc'
        assert root.parent == base / 'CatQC' / 'benchmark_runs'
        matches, incompatible = scan_matching_runs(root.parent, manifest['request_identity_sha256'])
        assert len(matches) == 1 and not incompatible
        assert load_core(root)[0]['run_id'] == manifest['run_id']
        acquire_lease(root, 'owner-a')
        heartbeat(root, 'owner-a')
        original = (root / 'run_manifest.json').read_bytes()
        snapshot = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file() and p.name != 'run_manifest.json'}
        for identity in ('legacy-benchmark', None):
            foreign = dict(manifest, skill_name=identity)
            atomic_write_json(root / 'run_manifest.json', foreign)
            matches, incompatible = scan_matching_runs(root.parent, manifest['request_identity_sha256'])
            assert not matches and len(incompatible) == 1
            expect_error(lambda: load_core(root), 'not a CatQC run')
            expect_error(lambda: acquire_lease(root, 'owner-a'), 'not a CatQC run')
            expect_error(lambda: heartbeat(root, 'owner-a'), 'not a CatQC run')
            assert not validate(root)['passed']
            assert snapshot == {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file() and p.name != 'run_manifest.json'}
        (root / 'run_manifest.json').write_bytes(original)
        wrong = dict(manifest, run_base=str(base / 'old-layout' / 'benchmark_runs'))
        atomic_write_json(root / 'run_manifest.json', wrong)
        assert not scan_matching_runs(root.parent, manifest['request_identity_sha256'])[0]
        expect_error(lambda: load_core(root), 'recorded direct child')
        assert not validate(root)['passed']
        (root / 'run_manifest.json').write_bytes(original)
        assert load_core(root)[0]['skill_name'] == 'catqc'
        # A minimal fixture proves the language check rejects actual non-English text.
        check = base / 'english-check'
        check.mkdir()
        (check / 'SKILL.md').write_text('---\nname: catqc\n---\nEnglish instructions.\n', encoding='utf-8')
        assert validate_package(check) == []
        (check / 'README.md').write_text(chr(0x4e2d), encoding='utf-8')
        assert any('must be English' in e for e in validate_package(check))
    print('CatQC identity/path/language self-test passed (6 scenario groups)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
