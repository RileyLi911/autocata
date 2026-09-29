from __future__ import annotations

import argparse
import tempfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from common import atomic_write_json, load_json, sha256_file
from environment_preparation_control import capture_guide
from model_usage_control import UsageError, freeze_usage_plan, validate_smoke_test, validate_usage_plan


from catalysis_test_support import model as catalysis_model, observation, plan_binding

def run_test(root: Path) -> None:
    identity = catalysis_model()
    atomic_write_json(root / "models_manifest.json", {"model_selection_mode": "user_specified", "models": [identity]})
    model_root = root / "models" / "fixture-model"
    source = root / "official-usage.md"
    source.write_text(identity["catalysis_assessment"]["sources"][0]["content"], encoding="utf-8")
    capture_guide(SimpleNamespace(
        model_root=model_root, kind="repository_readme",
        source_url=identity["catalysis_assessment"]["sources"][0]["url"], official_host=["github.com"],
        source_path=source, attested_by="user", revision=identity["catalysis_assessment"]["commit_sha"],
        used_for=["script_generation", "smoke_test", "runtime_execution"], guide_scope="usage",
        authority="official_repository",
    ))
    plan_source = root / "usage-plan-source.json"
    atomic_write_json(plan_source, {
        **plan_binding(identity),
        "source_guide_paths": [
            "models/fixture-model/resources/official_guides/usage/README-" + ""  # replaced below
        ],
        "official_imports": ["fixture.package"],
        "checkpoint_loading": {"source": "official", "variant": "fixture", "verification": "sha256", "path": "models/fixture-model/checkpoints/fixture.pt"},
        "calculator_factory": {"class": "fixture.Calculator"},
        "device_policy": {"device": "cuda"},
        "dtype_policy": {"dtype": "float32"},
        "energy_units": {"energy": "eV", "force": "eV/A"},
        "single_point_protocol": {"entrypoint": "fixture.sp"},
        "relaxation_protocol": {"entrypoint": "fixture.relax"},
        "official_smoke_tests": ["import", "gpu"],
        "derived_adaptations": [{"source": "official smoke command", "change": "write benchmark JSON", "reason": "preserve evidence"}],
    })
    record = load_json(model_root / "environment_preparation.json")
    guide_path = record["official_guides"][0]["artifact_path"]
    plan = load_json(plan_source)
    plan["source_guide_paths"] = [guide_path]
    atomic_write_json(plan_source, plan)
    result = freeze_usage_plan(SimpleNamespace(model_root=model_root, plan_source=plan_source))
    assert result["plan_sha256"]
    assert validate_usage_plan(model_root)["passed"]
    checkpoint = model_root / "checkpoints" / "fixture.pt"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_bytes(b"fixture checkpoint")
    stdout = model_root / "validation" / "official_usage_smoke_test.stdout.txt"
    stderr = model_root / "validation" / "official_usage_smoke_test.stderr.txt"
    stdout.parent.mkdir(parents=True, exist_ok=True)
    stdout.write_text("official smoke passed\n", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    frozen = load_json(model_root / "resources" / "model_usage_plan.json")
    atomic_write_json(model_root / "validation" / "official_usage_smoke_test.json", {
        **observation(root, identity),
        "schema_version": 4, "model_key": "fixture-model", "passed": True,
        "command": "python official_smoke.py", "python_executable": "fixture-python",
        "imports": ["fixture.package"], "checkpoint_path": "models/fixture-model/checkpoints/fixture.pt",
        "checkpoint_variant": "fixture", "checkpoint_sha256": sha256_file(checkpoint),
        "calculator_class": "fixture.Calculator", "device": "cuda", "dtype": "float32",
        "energy_unit": "eV", "force_unit": "eV/A", "exit_code": 0,
        "stdout_path": "models/fixture-model/validation/official_usage_smoke_test.stdout.txt",
        "stderr_path": "models/fixture-model/validation/official_usage_smoke_test.stderr.txt",
        "started_at": "2026-08-18T00:00:00+00:00", "finished_at": "2026-08-18T00:00:01+00:00",
        "guide_paths": frozen["source_guide_paths"], "guide_sha256": frozen["source_guide_hashes"],
        "plan_sha256": frozen["plan_sha256"], "plan_file_sha256": sha256_file(model_root / "resources" / "model_usage_plan.json"),
        "adaptations": [{"source": "official smoke command", "change": "write benchmark JSON", "reason": "preserve evidence"}],
    })
    assert validate_smoke_test(model_root)["passed"]
    original_smoke = load_json(model_root / "validation" / "official_usage_smoke_test.json")
    for field, value in (("parameters", {}), ("checkpoint_id", "wrong-checkpoint"), ("head", "wrong-head")):
        altered = deepcopy(original_smoke)
        altered["calculator_initializations"][0]["execution_variant"][field] = value
        atomic_write_json(model_root / "validation" / "official_usage_smoke_test.json", altered)
        assert not validate_smoke_test(model_root)["passed"]
    atomic_write_json(model_root / "validation" / "official_usage_smoke_test.json", original_smoke)
    smoke = load_json(model_root / "validation" / "official_usage_smoke_test.json")
    smoke["device"] = "cpu"
    atomic_write_json(model_root / "validation" / "official_usage_smoke_test.json", smoke)
    assert not validate_smoke_test(model_root)["passed"]
    guide_file = root / guide_path
    guide_file.write_text("tampered", encoding="utf-8")
    assert not validate_usage_plan(model_root)["passed"]
    try:
        freeze_usage_plan(SimpleNamespace(model_root=model_root, plan_source=plan_source))
    except UsageError:
        pass
    else:
        raise AssertionError("tampered usage guide must block plan freeze")
    print("model usage self-test passed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-root", type=Path)
    args = parser.parse_args()
    if args.fixture_root:
        args.fixture_root.mkdir(parents=True, exist_ok=True)
        run_test(args.fixture_root.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="model-usage-") as temporary:
            run_test(Path(temporary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
