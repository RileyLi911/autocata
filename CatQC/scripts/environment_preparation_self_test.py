from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from common import atomic_write_json
from environment_preparation_control import PreparationError, capture_guide, freeze_plan, select_toolchain, validate_record
from environment_install_control import plan_run
from probe_environment import probe


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="mlp-environment-preparation-") as temporary:
        root = Path(temporary) / "benchmark"
        model_root = root / "models" / "fixture-model"
        model_root.mkdir(parents=True)
        source = Path(temporary) / "official-readme.md"
        source.write_text("official install command: uv sync --locked\n", encoding="utf-8")
        try:
            capture_guide(argparse.Namespace(
                model_root=model_root, source_url="https://example.invalid/README.md", source_path=source,
                kind="repository_readme", authority="official_repository", revision="fixture", used_for=["environment-creation"],
                attested_by=None, official_host=["example.invalid"],
            ))
        except PreparationError:
            pass
        else:
            raise AssertionError("unattested local guide was accepted")
        guide_result = capture_guide(argparse.Namespace(
            model_root=model_root, source_url="https://example.invalid/README.md", source_path=source,
            kind="repository_readme", authority="official_repository", revision="fixture", used_for=["environment-creation"],
            attested_by="user", official_host=["example.invalid"],
        ))
        snapshot_path = Path(temporary) / "host.json"
        snapshot_path.write_text(json.dumps(probe()), encoding="utf-8")
        select_toolchain(argparse.Namespace(model_root=model_root, host_snapshot=snapshot_path))
        plan_path = Path(temporary) / "installation-plan.json"
        atomic_write_json(plan_path, {
            "source_guide_paths": [guide_result["artifact"]["artifact_path"]], "python": {"version": "3.11"},
            "framework": "pytorch", "gpu_required": True,
            "commands": ["uv sync --locked"], "packages": [{"name": "fixture", "version": "1"}],
            "environment_variables": {}, "smoke_tests": ["python -c import fixture"],
        })
        freeze_plan(argparse.Namespace(model_root=model_root, plan_source=plan_path))
        result = validate_record(argparse.Namespace(model_root=model_root))
        assert result["valid"] is True and result["status"] in {"ready_for_creation", "blocked"}
        if result["status"] == "ready_for_creation":
            execution = plan_run(model_root, Path(temporary) / "env")
            assert execution["status"] == "planned" and execution["commands"]
        record = json.loads((model_root / "environment_preparation.json").read_text(encoding="utf-8"))
        assert record["mutation_authorization"].get("policy") in {None, "ask_once"}


def main() -> int:
    run()
    print("environment preparation self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
