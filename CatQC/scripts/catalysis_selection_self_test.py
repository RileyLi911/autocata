"""Synthetic regression tests for official-source selection and execution gates."""
from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace

from common import atomic_write_json, canonical_json_sha256, load_json, frame_paths, validate_frame_artifact
from catalysis_selection import validate_selection, observation_errors, binding_errors
from catalysis_test_support import fields, model, observation, plan_binding
from run_control import build_models_manifest, request_identity, create_run, load_core, ControlError
from model_usage_control import freeze_usage_plan, validate_usage_plan, validate_smoke_test, UsageError
from environment_preparation_control import capture_guide


def payload():
    return {"schema_version": 4, "model_selection_mode": "user_specified", "models": [{
        "requested_name": "Example", "confirmed_name": "Example surface", "model_key": "fixture-model",
        "version_or_variant": "v1", "identity_url": "https://github.com/fixture/model", "source_kind": "repository",
        "confirmed": True, "confirmed_by": "user", "confirmed_at": "2026-09-10T00:00:00+00:00", "snapshot_rank": None,
        **fields(),
    }]}


def expect_failure(call, fragment):
    try:
        call()
    except (ControlError, UsageError, ValueError) as exc:
        assert fragment.lower() in str(exc).lower(), str(exc)
    else:
        raise AssertionError("expected failure: " + fragment)


def run_tests(root):
    root.mkdir(parents=True, exist_ok=True)
    raw = payload()
    original = build_models_manifest(raw, "fixture")
    assert original["models"][0]["execution_variant"]["parameters"] == {"domain": "surface"}
    cases = 1
    for mutate, fragment in (
        (lambda m: m.pop("execution_variant"), "original skill"),
        (lambda m: m["catalysis_assessment"].update(finding="insufficient_evidence"), "insufficient"),
        (lambda m: m["catalysis_assessment"].update(unresolved_conflicts=["code differs from documentation"]), "conflicts"),
        (lambda m: m["catalysis_assessment"].update(checked_version="old"), "checked_version"),
        (lambda m: m["catalysis_assessment"]["sources"][0].update(content="changed text"), "hash mismatch"),
        (lambda m: m["catalysis_assessment"]["sources"][0].update(url="https://github.com/unofficial/fork/blob/" + "a"*40 + "/README.md"), "verified repository"),
        (lambda m: m["catalysis_assessment"]["candidates"][0]["energy_semantics"].update(kind="adsorption_energy"), "three-total-energy"),
        (lambda m: m["catalysis_assessment"]["candidates"][0].update(compatibility="incompatible"), "incompatible"),
        (lambda m: m["catalysis_assessment"]["decision"].update(confirmed=False), "confirmation"),
        (lambda m: m["execution_variant"].update(parameters={}), "selected candidate"),
    ):
        broken = deepcopy(raw)
        mutate(broken["models"][0])
        expect_failure(lambda: build_models_manifest(broken, "fixture"), fragment)
        cases += 1
    general = deepcopy(raw)
    general["models"][0]["catalysis_assessment"]["finding"] = "no_specialized_found_in_checked_sources"
    assert not validate_selection(general["models"][0])
    cases += 1

    nonrecommended = deepcopy(raw)
    a = nonrecommended["models"][0]["catalysis_assessment"]
    alternate = deepcopy(a["candidates"][0])
    alternate.update(id="general")
    alternate["execution_variant"]["parameters"] = {"domain": "general"}
    a["candidates"].append(alternate)
    a["decision"].update(selected_candidate_id="general", reason="user retains compatible general task for controlled comparison")
    nonrecommended["models"][0]["execution_variant"] = deepcopy(alternate["execution_variant"])
    changed = build_models_manifest(nonrecommended, "fixture")
    assert request_identity("data", "NO3", ["single_point"], original) != request_identity("data", "NO3", ["single_point"], changed)
    cases += 1

    leaderboard = deepcopy(raw)
    leaderboard["model_selection_mode"] = "matbench_top_n"
    m = leaderboard["models"][0]
    m["snapshot_rank"] = 1
    m["leaderboard_identity"] = {"model_name": "Original leaderboard model", "version_or_variant": "older-release",
        "source_url": "https://example.invalid/leaderboard", "snapshot_rank": 1,
        "execution_variant": {"checkpoint_id": "original-weight", "parameters": {"domain": "bulk"}, "head": "bulk"}}
    assert build_models_manifest(leaderboard, "fixture")["models"][0]["leaderboard_identity"]["execution_variant"]["checkpoint_id"] == "original-weight"
    m["catalysis_assessment"]["decision"]["confirmed"] = False
    expect_failure(lambda: build_models_manifest(leaderboard, "fixture"), "confirmation")
    cases += 1

    run = create_run(root / "workspace", Path(__file__).parents[1], "fixture", "NO3", ["single_point"], "fixture", raw, "test-owner")
    manifest = load_json(run / "models_manifest.json")
    identity = manifest["models"][0]
    source = identity["catalysis_assessment"]["sources"][0]
    archive = run / "selection_evidence/catalysis" / (source["sha256"] + ".txt")
    assert archive.read_text(encoding="utf-8") == source["content"]
    load_core(run)
    archive.write_text("tampered", encoding="utf-8")
    expect_failure(lambda: load_core(run), "source evidence")
    archive.write_bytes(source["content"].encode("utf-8"))
    cases += 1

    obs = observation(run, identity)
    assert not observation_errors(obs, identity, run)
    for component in range(3):
        bad = deepcopy(obs)
        bad["calculator_initializations"][component]["execution_variant"]["parameters"] = {"domain": "molecule"}
        assert any("configuration mismatch" in x for x in observation_errors(bad, identity, run))
        cases += 1
    for mutate in (
        lambda x: x["execution_variant"].update(checkpoint_id="other"),
        lambda x: x["calculator_initializations"][0]["inspection"].update(parameters={}),
        lambda x: x["calculator_initializations"][0].update(checkpoint_sha256="0"*64),
        lambda x: x.update(calculator_initializations=[]),
    ):
        bad=deepcopy(obs); mutate(bad)
        assert observation_errors(bad, identity, run)
        cases += 1

    # A real frame validation must reject component mixing, even if the frame
    # has valid output files and lifecycle evidence.
    from frame_artifact_self_test import write_frame
    write_frame(run, "single_point", 0)
    p = frame_paths(run, "fixture-model", "single_point", 0)["manifest"]
    fm = load_json(p); fm.update(obs); atomic_write_json(p, fm)
    assert validate_frame_artifact(run, "fixture-model", "single_point", 0)["valid"]
    fm["calculator_initializations"][2]["execution_variant"]["head"] = "molecule"
    atomic_write_json(p, fm)
    assert not validate_frame_artifact(run, "fixture-model", "single_point", 0)["valid"]
    cases += 1

    # Freeze usage against the same archived GitHub content, then check revision
    # drift and independently mutated task/checkpoint selections.
    mr = run / "models/fixture-model"
    capture_guide(SimpleNamespace(model_root=mr, kind="repository_readme", source_url=source["url"],
        official_host=["github.com"], source_path=archive, attested_by="user", revision=source["commit_sha"],
        used_for=["script_generation", "smoke_test", "runtime_execution"], guide_scope="usage", authority="official_repository"))
    guide = load_json(mr / "environment_preparation.json")["official_guides"][0]
    plan={**plan_binding(identity), "source_guide_paths":[guide["artifact_path"]], "official_imports":["fixture"],
          "checkpoint_loading":{"source":"official", "variant":"fixture", "verification":"sha256", "path":"models/fixture-model/checkpoints/fixture.pt"},
          "calculator_factory":{"class":"fixture.Calculator"},"device_policy":{"device":"cuda"},"dtype_policy":{"dtype":"float32"},
          "energy_units":{"energy":"eV","force":"eV/A"},"single_point_protocol":{"entrypoint":"sp"},"relaxation_protocol":{"entrypoint":"relax"},
          "official_smoke_tests":["fixture"],"derived_adaptations":[{"source":"fixture","change":"I/O","reason":"audit"}]}
    plan_source = root / "usage.json"; atomic_write_json(plan_source,plan)
    freeze_usage_plan(SimpleNamespace(model_root=mr,plan_source=plan_source))
    assert validate_usage_plan(mr)["passed"]
    bad=deepcopy(plan);bad["execution_variant"]["parameters"]={}
    assert binding_errors(mr,bad)
    prep=load_json(mr/'environment_preparation.json');prep['official_guides'][0]['revision']='b'*40
    atomic_write_json(mr/'environment_preparation.json',prep)
    assert not validate_usage_plan(mr)["passed"]
    prep['official_guides'][0]['revision']=source['commit_sha'];atomic_write_json(mr/'environment_preparation.json',prep)
    other=mr/'checkpoints/other.pt';other.write_bytes(b'other')
    bad=deepcopy(obs);bad['calculator_initializations'][0]['checkpoint_path']=other.relative_to(run).as_posix()
    assert any('frozen usage plan' in e for e in observation_errors(bad,identity,run))
    cases += 3
    print(f"Catalysis selection self-test passed ({cases} scenario groups)")


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--fixture-root',type=Path);args=parser.parse_args()
    if args.fixture_root:
        run_tests(args.fixture_root.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix='catalysis-selection-test-') as temp:
            run_tests(Path(temp))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
