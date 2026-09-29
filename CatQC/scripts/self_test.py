from __future__ import annotations

import argparse
import csv
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from common import atomic_write_json, canonical_json_sha256, file_evidence, frame_paths, load_json, sha256_file
from run_control import (
    ControlError,
    acquire_lease,
    build_models_manifest,
    create_run,
    initialize_frames,
    init_or_resume,
    iso_time,
    reconcile_run,
    request_identity,
    scan_matching_runs,
    task_checkpoint_path,
    utc_now,
)
from validate_run import validate
from catalysis_test_support import fields as catalysis_fields, observation as catalysis_observation


def selection(count: int = 5, prefix: str = "Model") -> dict:
    return {
        "schema_version": 4,
        "model_selection_mode": "user_specified",
        "selection_source": "self-test-user-confirmation",
        "models": [
            {
                **catalysis_fields(f"v{index}"),
                "requested_name": f"{prefix} {index}",
                "confirmed_name": f"{prefix} {index} official",
                "version_or_variant": f"v{index}",
                "identity_url": f"https://example.invalid/{prefix.lower()}-{index}",
                "source_kind": "model_card",
                "confirmed": True,
                "confirmed_by": "user",
                "confirmed_at": "2026-08-17T00:00:00+00:00",
                "snapshot_rank": None,
                "source_evidence": [{"kind": "model_card", "url": f"https://example.invalid/{index}"}],
            }
            for index in range(1, count + 1)
        ],
    }


def write_result(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "structure_index", "slab_composition", "E_slab_plus_adsorbate_eV",
        "E_slab_eV", "E_adsorbate_eV", "E_adsorption_eV", "status", "error_message",
    ]
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def new_run(base: Path, skill_root: Path, payload: dict, owner: str = "owner-a", tasks: list[str] | None = None) -> Path:
    return create_run(
        base, skill_root, "fixture-dataset", "NO3", tasks or ["single_point", "relaxation"],
        "fixture request", payload, owner,
    )


def expect_error(callable_, fragment: str) -> None:
    try:
        callable_()
    except Exception as exc:
        assert fragment.lower() in str(exc).lower(), (fragment, str(exc))
    else:
        raise AssertionError(f"expected error containing {fragment!r}")


def write_frame_artifact(root: Path, run_id: str, model_key: str, task: str, index: int, *, status: str = "completed") -> None:
    paths = frame_paths(root, model_key, task, index)
    for key in ("input", "work", "outputs", "errors"):
        paths[key].mkdir(parents=True, exist_ok=True)
    paths["input_structure"].write_text("1\nfixture\nH 0 0 0\n", encoding="utf-8")
    input_hash = sha256_file(paths["input_structure"])
    atomic_write_json(paths["input_manifest"], {"structure_index": index, "input_structure_sha256": input_hash})
    atomic_write_json(paths["cleanup"], {
        "calculator_released": True, "cleanup_completed": True,
        "gpu_cache_cleared": True,
    })
    outputs = {}
    if status == "completed":
        atomic_write_json(paths["result"], {"structure_index": index, "status": "success"})
        outputs["result"] = file_evidence(paths["result"], root)
    else:
        atomic_write_json(paths["error"], {"structure_index": index, "reason": "fixture failure"})
    atomic_write_json(paths["manifest"], {
        **catalysis_observation(root, next(m for m in load_json(root / "models_manifest.json")["models"] if m["model_key"] == model_key)),
        "schema_version": 4, "run_id": run_id, "model_key": model_key,
        "task": task, "structure_index": index,
        "input_structure_sha256": input_hash, "status": status,
        "attempt_count": 1,
        "lifecycle": {
            "calculator_created": True, "calculator_fresh": True,
            "calculator_released": True, "cleanup_completed": True,
            "mutable_state_reused": False,
        },
        "error": file_evidence(paths["error"], root) if status != "completed" else None,
        "outputs": outputs, "created_at": iso_time(), "updated_at": iso_time(),
    })


def run_tests(fixture: Path, skill_root: Path) -> None:
    fixture.mkdir(parents=True, exist_ok=True)
    payload = selection()

    # Five models + NO3 + SP/Relax naming and complete initial v4 control layout.
    base_a = fixture / "naming"
    root_a = new_run(base_a, skill_root, payload)
    assert root_a.name.endswith("_5models_no3_sp-relax")
    for relative in (
        "run_manifest.json", "models_manifest.json", "run_state.json", "events.jsonl",
        "control/lease.json", "selection_evidence/model_selection_input.json",
    ):
        assert (root_a / relative).is_file(), relative
    for model in load_json(root_a / "models_manifest.json")["models"]:
        for task in ("single_point", "relaxation"):
            assert (root_a / task_checkpoint_path(model["model_key"], task)).is_file()
    assert validate(root_a)["passed"], validate(root_a)

    # Same-count different identities never match.
    manifest_a = build_models_manifest(payload, "identity")
    identity_a = request_identity("fixture-dataset", "NO3", ["single_point", "relaxation"], manifest_a)
    identity_b = request_identity(
        "fixture-dataset", "NO3", ["single_point", "relaxation"],
        build_models_manifest(selection(prefix="Different"), "identity"),
    )
    assert identity_a != identity_b
    assert identity_a != request_identity(
        "fixture-dataset", "NO3", ["single_point", "relaxation"], manifest_a,
        dft_reference_mode="generate_vasp",
    )
    assert not scan_matching_runs(base_a / "CatQC" / "benchmark_runs", identity_b)[0]

    # One incomplete match is identifiable; two require an explicit selection.
    matches, _ = scan_matching_runs(base_a / "CatQC" / "benchmark_runs", identity_a)
    assert len(matches) == 1 and matches[0]["path"] == str(root_a.resolve())
    selection_file = fixture / "selection.json"
    atomic_write_json(selection_file, payload)
    init_args = SimpleNamespace(
        workspace_base=base_a, skill_root=skill_root, selection_file=selection_file,
        dataset_name="fixture-dataset", adsorbate="NO3",
        tasks=["single_point", "relaxation"], owner_id="owner-a",
        original_prompt="fixture request", new_run=False, select_run=None,
        external_jobs_reconciled=False,
    )
    assert init_or_resume(init_args)["action"] == "resumed"
    root_a2 = new_run(base_a, skill_root, payload)
    matches, _ = scan_matching_runs(base_a / "CatQC" / "benchmark_runs", identity_a)
    assert len(matches) == 2 and all("progress_fraction" in item for item in matches)
    selection_required = init_or_resume(init_args)
    assert selection_required["action"] == "selection_required" and len(selection_required["candidates"]) == 2
    complete_manifest = load_json(root_a / "run_manifest.json")
    complete_manifest["status"] = "complete"
    atomic_write_json(root_a / "run_manifest.json", complete_manifest)
    matches, _ = scan_matching_runs(base_a / "CatQC" / "benchmark_runs", identity_a)
    assert [item["path"] for item in matches] == [str(root_a2.resolve())]

    # Step 1 freezes frames and blocks an adsorbate mismatch without renaming.
    initialize_frames(root_a2, "owner-a", 3, "NO3")
    assert validate(root_a2)["passed"], validate(root_a2)
    mismatch = new_run(fixture / "mismatch", skill_root, selection(1), tasks=["single_point"])
    original_name = mismatch.name
    expect_error(lambda: initialize_frames(mismatch, "owner-a", 2, "CO"), "does not match")
    assert mismatch.name == original_name
    assert load_json(mismatch / "run_state.json")["adsorbate_validation"] == "mismatch"

    # Crash window A: valid result row exists but checkpoint was not updated.
    model_key = load_json(root_a2 / "models_manifest.json")["models"][0]["model_key"]
    result_path = root_a2 / "models" / model_key / "results" / "adsorption_energy_SP.csv"
    write_frame_artifact(root_a2, load_json(root_a2 / "run_manifest.json")["run_id"], model_key, "single_point", 0)
    write_result(result_path, [{
        "structure_index": 0, "slab_composition": "Cu", "E_slab_plus_adsorbate_eV": -11,
        "E_slab_eV": -9, "E_adsorbate_eV": -1, "E_adsorption_eV": -1,
        "status": "success", "error_message": "",
    }])
    checkpoint_path = root_a2 / task_checkpoint_path(model_key, "single_point")
    assert load_json(checkpoint_path)["completed_indices"] == []
    reconcile_run(root_a2, "owner-a")
    assert load_json(checkpoint_path)["completed_indices"] == [0]

    # Crash window B: checkpoint-only completion is removed and becomes pending.
    checkpoint = load_json(checkpoint_path)
    checkpoint["completed_indices"] = [0, 1]
    checkpoint["status"] = "running"
    atomic_write_json(checkpoint_path, checkpoint)
    reconcile_run(root_a2, "owner-a")
    rebuilt = load_json(checkpoint_path)
    assert rebuilt["completed_indices"] == [0] and rebuilt["next_index"] == 1

    # Valid foreign lease blocks; expired takeover reattaches a live job.
    job_path = root_a2 / "models" / model_key / "jobs" / "single_point_job_status.json"
    now = iso_time()
    atomic_write_json(job_path, {
        "schema_version": 4,
        "run_id": load_json(root_a2 / "run_manifest.json")["run_id"],
        "model_key": model_key,
        "task": "single_point",
        "run_method": "slurm",
        "job_id": "12345",
        "pid": None,
        "state": "running",
        "active_indices": [2],
        "submit_time": now,
        "last_checked": now,
        "exit_code": None,
        "log_path": f"models/{model_key}/logs/single_point.log",
        "result_path": f"models/{model_key}/results/adsorption_energy_SP.csv",
        "status_file_path": None,
        "scheduler_evidence": {"source": "self-test"},
    })
    reconcile_run(root_a2, "owner-a")
    expect_error(lambda: acquire_lease(root_a2, "owner-b"), "active owner")
    lease_path = root_a2 / "control" / "lease.json"
    lease = load_json(lease_path)
    lease["expires_at"] = iso_time(utc_now() - timedelta(seconds=1))
    atomic_write_json(lease_path, lease)
    _, takeover = acquire_lease(root_a2, "owner-b")
    assert takeover
    state = load_json(root_a2 / "run_state.json")
    assert state["reconciliation_required"] and not state["submission_allowed"]
    report = reconcile_run(root_a2, "owner-b", external_jobs_reconciled=True)
    resumed_state = load_json(root_a2 / "run_state.json")
    assert not resumed_state["reconciliation_required"] and not resumed_state["submission_allowed"]
    assert resumed_state["next_action"] == "monitor_active_jobs" and resumed_state["active_jobs"][0]["job_id"] == "12345"
    assert (root_a2 / "control" / "latest_reconciliation.json").is_file()
    assert report["external_jobs_reconciled"] is True
    job = load_json(job_path)
    job["state"] = "completed"
    job["active_indices"] = []
    job["exit_code"] = 0
    job["last_checked"] = iso_time()
    atomic_write_json(job_path, job)
    reconcile_run(root_a2, "owner-b", external_jobs_reconciled=True)

    # A fully appended event with a stale central sequence is recovered safely.
    state_before = load_json(root_a2 / "run_state.json")
    orphan_sequence = state_before["last_event_sequence"] + 1
    with (root_a2 / "events.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({
            "schema_version": 4, "sequence": orphan_sequence, "timestamp": iso_time(),
            "owner_id": "owner-b", "event_type": "simulated_post_append_crash",
        }, sort_keys=True) + "\n")
    reconcile_run(root_a2, "owner-b", external_jobs_reconciled=True)
    assert load_json(root_a2 / "run_state.json")["last_event_sequence"] > orphan_sequence

    # Manifest hash drift and truncated event logs are hard validation failures.
    models_path = root_a2 / "models_manifest.json"
    original_models_text = models_path.read_text(encoding="utf-8")
    drifted = json.loads(original_models_text)
    drifted["models"][0]["model_name"] = "tampered"
    atomic_write_json(models_path, drifted)
    assert any("hash" in error for error in validate(root_a2)["errors"])
    models_path.write_text(original_models_text, encoding="utf-8")
    events_path = root_a2 / "events.jsonl"
    original_events = events_path.read_text(encoding="utf-8")
    events_path.write_text(original_events.rstrip("\n"), encoding="utf-8")
    assert any("truncated" in error for error in validate(root_a2)["errors"])
    events_path.write_text(original_events, encoding="utf-8")

    # Duplicate result indices and v2/v3 runs are explicitly rejected.
    write_result(result_path, [
        {"structure_index": 0, "slab_composition": "Cu", "E_slab_plus_adsorbate_eV": -11, "E_slab_eV": -9, "E_adsorbate_eV": -1, "E_adsorption_eV": -1, "status": "success", "error_message": ""},
        {"structure_index": 0, "slab_composition": "Cu", "E_slab_plus_adsorbate_eV": -11, "E_slab_eV": -9, "E_adsorbate_eV": -1, "E_adsorption_eV": -1, "status": "success", "error_message": ""},
    ])
    expect_error(lambda: reconcile_run(root_a2, "owner-b"), "duplicate")
    old = fixture / "old" / "CatQC" / "benchmark_runs" / "20260817-000000_1models_no3_sp"
    old.mkdir(parents=True)
    atomic_write_json(old / "run_manifest.json", {"schema_version": 3})
    old_result = validate(old)
    assert not old_result["passed"] and any("incompatible" in error.lower() for error in old_result["errors"])

    print("v4 run-control self-test passed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-root", type=Path)
    args = parser.parse_args()
    skill_root = Path(__file__).resolve().parents[1]
    if args.fixture_root:
        run_tests(args.fixture_root.resolve(), skill_root)
    else:
        with tempfile.TemporaryDirectory(prefix="mlp-benchmark-v4-") as temporary:
            run_tests(Path(temporary), skill_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
