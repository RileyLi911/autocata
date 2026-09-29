from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from common import atomic_write_json, load_json
from dft_reference_control import (
    DFTControlError,
    build_submission,
    load_scheduler_evidence,
    parse_vasprun,
    parse_crosscheck_energy,
    validate_energy_crosschecks,
    validate_runtime_probe,
    classify_failure,
    inspect_output_diagnostics,
    potcar_build_record,
    prepare,
    reject_secrets,
    validate_provided,
    run_preflight,
)
from run_control import create_run, initialize_frames
from merge_benchmark_results import load_reference


from catalysis_test_support import fields as catalysis_fields

def selection() -> dict:
    return {
        "schema_version": 4,
        "model_selection_mode": "user_specified",
        "selection_source": "dft-self-test",
        "models": [{
            **catalysis_fields('v1'),
            "requested_name": "Fixture MLP", "confirmed_name": "Fixture MLP",
            "version_or_variant": "v1", "identity_url": "https://example.invalid/model",
            "source_kind": "model_card", "confirmed": True, "confirmed_by": "user",
            "confirmed_at": "2026-08-17T00:00:00+00:00", "snapshot_rank": None,
            "source_evidence": [{"kind": "model_card", "url": "https://example.invalid/model"}],
        }],
    }


def profile(backend: str = "vaspkit") -> dict:
    potcar = {
        "backend": backend, "functional_family": "PBE_fixture",
        "element_labels": {"Cu": "Cu", "N": "N", "O": "O"},
    }
    if backend == "vaspkit":
        potcar.update({"vaspkit_executable": "vaspkit", "vaspkit_task": 103,
                       "vaspkit_input_lines": ["Cu N O"], "validated_vaspkit_version": "fixture",
                       "vaspkit_version_command": "vaspkit --version"})
    else:
        potcar["potcar_root"] = "/authorized/potpaw_PBE"
    result = {
        "schema_version": 4, "profile_id": "fixture-pbe", "confirmed": True,
        "confirmed_by": "user", "confirmed_at": "2026-08-17T00:00:00+00:00", "incar": {"ENCUT": 400, "NELM": 60},
        "kpoints": {"mode": "gamma_only"}, "energy_source": "energy_sigma_to_zero",
        "output_validation": {"energy_tolerance_eV": 1e-5},
        "relaxation": {"fixed_cell": True, "force_threshold_eV_per_A": 0.05,
                       "slab_constraint_rule": "fix_bottom_1_layer_for_n_layers_lte_5_else_bottom_2_layers",
                       "layer_tolerance_A": 0.2, "max_ionic_steps": 200},
        "isolated_adsorbate": {"vacuum_A": 8.0, "charge": 0, "spin_multiplicity": 1,
                                "gamma_only": True, "pbc": True}, "potcar_generation": potcar,
    }
    from dft_profile import payload
    from common import canonical_json_sha256
    result['incar'].update(IBRION=2, ISPIN=2)
    result['potcar_generation']['dataset_version'] = 'synthetic-fixture'
    result['components'] = {k: {'charge':0, 'ispin':2,
        'initial_magmoms_by_element': {'Cu':0, 'N':1, 'O':0}} for k in
        ('slab_adsorbate', 'clean_slab', 'isolated_adsorbate')}
    result['selection_provenance'] = {'source':'explicit synthetic test fixture'}
    result['confirmation_sha256'] = canonical_json_sha256(payload(result))
    return result



def execution_profile() -> dict:
    return {
        "schema_version": 4, "backend": "dpdispatcher", "credentials_policy": "external_only",
        "machine": {"batch_type": "Slurm", "context_type": "SSHContext", "local_root": ".", "remote_root": "/remote/work"},
        "resources": {"number_node": 1, "cpu_per_node": 4, "gpu_per_node": 0, "group_size": 1},
        "vasp_command": "vasp_std", "forward_files": ["inputs/POSCAR", "inputs/INCAR", "inputs/KPOINTS", "inputs/POTCAR.spec"],
        "backward_files": ["vasprun.xml", "OUTCAR", "OSZICAR", "CONTCAR", "POTCAR.titel", "POTCAR.sha256"],
        "runtime_preflight": {"stack_limit_policy": "required_unlimited", "apply_in_submission_shell": True, "apply_in_job_shell": True, "verify_after_launch": True, "mpi_launcher": "mpirun", "mpi_version_command": "mpirun --version", "vasp_version_command": "vasp_std --version"},
        "resource_policy": {"cpu_per_task": 8, "max_concurrent_tasks": 3, "oversubscription": "forbidden"},
        "timeout_policy": {"dft_walltime": "none", "dft_per_calculation_timeout": "none", "dft_idle_timeout": "none", "automatic_timeout_cancel": False},
        "output_monitor": {
            "enabled": True, "poll_interval_seconds": 60,
            "termination_rules": {"fatal_process_error": True, "electronic_oscillation": "user_confirmed", "corrupted_output": True, "missing_output": False},
            "electronic_oscillation": {"enabled": True, "window_steps": 12, "minimum_observed_steps": 20, "required_pattern": "repeated_non_decreasing_residual_with_energy_sign_changes", "action": "terminate_and_block", "marker_patterns": ["BRMIX", "EDDDAV", "electronic step oscillation"]},
        },
    }


def create_fixture_run(base: Path, skill_root: Path, mode: str, owner: str) -> Path:
    return create_run(base, skill_root, "fixture", "NO3", ["relaxation"], "fixture", selection(), owner,
                      dataset_identity="fixture-v1", dft_reference_mode=mode)


def write_reference(path: Path, count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["structure_index", "slab_composition", "dft_adsorption_energy_eV"])
        writer.writeheader()
        for index in range(count):
            writer.writerow({"structure_index": index, "slab_composition": "Cu", "dft_adsorption_energy_eV": -1.0 - index})


def run_tests(fixture: Path, skill_root: Path) -> None:
    fixture.mkdir(parents=True, exist_ok=True)
    try:
        reject_secrets({"machine": {"password": "forbidden"}})
    except DFTControlError:
        pass
    else:
        raise AssertionError("credential field was not rejected")

    direct = potcar_build_record(profile("direct"), ["Cu", "Cu", "N", "O"])
    assert direct["element_order"] == ["Cu", "N", "O"] and direct["command"].startswith("cat ")
    kit = potcar_build_record(profile("vaspkit"), ["Cu", "N", "O"])
    assert kit["backend"] == "vaspkit" and "-task 103" in kit["command"]

    xml = fixture / "vasprun.xml"
    xml.write_text("""<modeling><calculation><scstep/><energy><i name=\"e_0_energy\">-12.5</i></energy><varray name=\"forces\"><v>0.01 0 0</v><v>0 0.02 0</v></varray></calculation></modeling>""", encoding="utf-8")
    parsed = parse_vasprun(xml, profile())
    assert parsed["energy_eV"] == -12.5 and parsed["ionic_converged"] and parsed["electronic_converged"]
    (fixture / "OUTCAR").write_text("EDIFF is reached\nreached required accuracy - stopping structural energy minimisation\nenergy(sigma->0) = -12.500000\n", encoding="utf-8")
    (fixture / "OSZICAR").write_text(" 1 F= -12.500000 E0= -12.500000\n", encoding="utf-8")
    checked = validate_energy_crosschecks(-12.5, fixture, profile())
    assert checked["outcar_energy_eV"] == -12.5 and checked["oszicar_energy_eV"] == -12.5
    (fixture / "OSZICAR").write_text(" 1 F= -12.400000 E0= -12.400000\n", encoding="utf-8")
    try:
        validate_energy_crosschecks(-12.5, fixture, profile())
    except DFTControlError:
        pass
    else:
        raise AssertionError("inconsistent OUTCAR/OSZICAR energy was accepted")

    execution = execution_profile()
    probe = {"schema_version": 4, "run_id": "fixture-run", "passed": True,
             "submission_shell_stack": "unlimited", "job_shell_stack": "unlimited",
             "vasp_version": "VASP 5.4.4", "mpi_launcher": "mpirun", "mpi_version": "fixture",
             "cpu_available": 24, "memory_bytes": 128 * 1024**3,
             "smoke_test": {"passed": True}}
    allocation = validate_runtime_probe(probe, execution, "fixture-run", 24, 3)
    assert allocation["cpu_per_task"] == 8 and allocation["planned_concurrent_tasks"] == 3
    bad_probe = dict(probe, job_shell_stack="8192 KB")
    try:
        validate_runtime_probe(bad_probe, execution, "fixture-run", 24, 3)
    except DFTControlError:
        pass
    else:
        raise AssertionError("non-unlimited job stack was accepted")
    diagnostic_dir = fixture / "diagnostic-output"
    diagnostic_dir.mkdir(parents=True, exist_ok=True)
    (diagnostic_dir / "stderr").write_text("SIGSEGV: soft stack limit 8192 KB", encoding="utf-8")
    assert classify_failure(diagnostic_dir)[0] == "soft_stack_sigsegv"
    idle_dir = fixture / "idle-output"
    (idle_dir / "outputs").mkdir(parents=True, exist_ok=True)
    idle_diagnostic = inspect_output_diagnostics(idle_dir, execution, {"state": "running"})
    assert idle_diagnostic["termination_requested"] is False
    oscillation_dir = fixture / "oscillation-output"
    (oscillation_dir / "outputs").mkdir(parents=True, exist_ok=True)
    oscillation_dir.joinpath("outputs", "OUTCAR").write_text("BRMIX\n" + "\n".join("DAV: 1" for _ in range(20)), encoding="utf-8")
    oscillation_diagnostic = inspect_output_diagnostics(oscillation_dir, execution, {"state": "running"})
    assert oscillation_diagnostic["termination_requested"] is True

    evidence_path = fixture / "scheduler-evidence.json"
    atomic_write_json(evidence_path, {
        "schema_version": 4, "run_id": "fixture-run", "queried_at": "2026-08-17T00:00:00+00:00",
        "source": "fixture-scheduler", "jobs": [
            {"calculation_id": "calc-a", "state": "running", "job_id": "123"},
            {"calculation_id": "calc-b", "state": "unknown", "message": "not visible"},
        ],
    })
    evidence = load_scheduler_evidence(evidence_path, "fixture-run", {"calc-a", "calc-b"})
    assert evidence["jobs"][1]["state"] == "unknown"
    try:
        load_scheduler_evidence(evidence_path, "fixture-run", {"calc-a", "calc-b", "calc-c"})
    except DFTControlError:
        pass
    else:
        raise AssertionError("incomplete scheduler evidence was accepted")

    partial = fixture / "partial-reference.csv"
    write_reference(partial, 1)
    assert sorted(load_reference(partial, 2, allow_partial=True)) == [0]
    try:
        load_reference(partial, 2)
    except ValueError:
        pass
    else:
        raise AssertionError("unapproved partial DFT reference was accepted")

    owner = "dft-self-test"
    provided_root = create_fixture_run(fixture / "provided", skill_root, "provided", owner)
    initialize_frames(provided_root, owner, 2, "NO3")
    source = fixture / "provided-source.csv"
    write_reference(source, 2)
    atomic_write_json(provided_root / "benchmark_config.json", {
        "benchmark_request": {"dft_reference_mode": "provided"},
        "frame_count": 2, "dft_reference_path": str(source),
    })
    evidence = validate_provided(provided_root, owner)
    assert evidence["valid_frames"] == 2
    assert load_json(provided_root / "run_state.json")["dft_reference"]["status"] == "provided_ready"

    try:
        from ase import Atoms
        from ase.io import write
    except ImportError:
        print("DFT self-test passed (ASE preparation skipped: ASE unavailable)")
        return
    generated_root = create_fixture_run(fixture / "generated", skill_root, "generate_vasp", owner)
    initialize_frames(generated_root, owner, 2, "NO3")
    symbols = ["Cu", "Cu", "Cu", "Cu", "N", "O", "O", "O"]
    positions = [(1, 1, 1), (3, 1, 1), (1, 3, 1), (3, 3, 1), (2, 2, 3), (2.8, 2, 3), (1.6, 2.7, 3), (1.6, 1.3, 3)]
    frames = [Atoms(symbols, positions=positions, cell=[8, 8, 12], pbc=True),
              Atoms(symbols, positions=[(x + (0.1 if i >= 4 else 0), y, z) for i, (x, y, z) in enumerate(positions)], cell=[8, 8, 12], pbc=True)]
    structures = fixture / "structures-poscar"
    structures.mkdir(parents=True, exist_ok=True)
    write(structures / "02.POSCAR", frames[1], format="vasp", direct=True, vasp5=True, sort=False)
    write(structures / "01.POSCAR", frames[0], format="vasp", direct=True, vasp5=True, sort=False)
    atomic_write_json(generated_root / "benchmark_config.json", {
        "benchmark_request": {"dft_reference_mode": "generate_vasp"},
        "frame_count": 2, "structure_path": str(structures),
        "dft_reference": {"structure_derivation": {"adsorbate_indices": [4, 5, 6, 7]}},
    })
    profile_path = fixture / "vasp-profile.json"
    execution_path = fixture / "execution-profile.json"
    atomic_write_json(profile_path, profile())
    atomic_write_json(execution_path, execution_profile())
    manifest = prepare(generated_root, owner, profile_path, execution_path)
    assert len(manifest["frame_mapping"]) == 2 and len(manifest["calculations"]) == 4
    assert manifest["structure_input"]["file_type"] == "poscar"
    assert [Path(item["path"]).name for item in manifest["structure_input"]["source_files"]] == ["01.POSCAR", "02.POSCAR"]
    atomic_write_json(generated_root / "dft" / "runtime_probe.json", {
        "schema_version": 4, "run_id": load_json(generated_root / "run_manifest.json")["run_id"], "passed": True,
        "submission_shell_stack": "unlimited", "job_shell_stack": "unlimited",
        "vasp_version": "VASP 5.4.4", "mpi_launcher": "mpirun", "mpi_version": "Open MPI fixture",
        "cpu_available": 32, "memory_bytes": 128 * 1024**3,
        "smoke_test": {"passed": True, "calculation_id": "clean_slab-fixture", "exit_code": 0},
    })
    preflight = run_preflight(generated_root, owner)
    assert preflight["status"] == "passed" and preflight["resource_allocation"]["cpu_per_task"] == 8
    descriptor = build_submission(generated_root, owner)
    assert len(descriptor["tasks"]) == 4
    assert all("ulimit -s unlimited" in task["wrapper"] for task in descriptor["tasks"])
    assert all(task["wrapper_sha256"] for task in descriptor["tasks"])
    descriptor_text = json.dumps(descriptor, ensure_ascii=False).lower()
    assert "--time" not in descriptor_text and "--time-limit" not in descriptor_text
    assert descriptor["timeout_policy"]["dft_walltime"] == "none"
    assert all("POTCAR.titel" in task["command"] and "POTCAR.sha256" in task["command"] for task in descriptor["tasks"])
    assert all("POTCAR" not in [Path(item).name for item in execution_profile()["backward_files"]] for _ in [0])
    print("DFT self-test passed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-root", type=Path, required=True)
    args = parser.parse_args()
    run_tests(args.fixture_root.resolve(), Path(__file__).resolve().parents[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
