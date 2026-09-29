from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from common import atomic_write_json, file_evidence, frame_paths, sha256_file, validate_frame_artifact


def write_frame(root: Path, task: str, index: int, status: str = "completed") -> None:
    paths = frame_paths(root, "fixture-model", task, index)
    for key in ("input", "work", "outputs", "errors"):
        paths[key].mkdir(parents=True, exist_ok=True)
    paths["input_structure"].write_text(f"1\nframe {index}\nH 0 0 {index}\n", encoding="utf-8")
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
        if task == "relaxation":
            paths["relaxed_structure"].write_text(
                f"1\nrelaxed frame {index}\nH 0 0 {index + 0.1}\n", encoding="utf-8"
            )
            outputs["relaxed_structure"] = file_evidence(paths["relaxed_structure"], root)
    else:
        atomic_write_json(paths["error"], {"structure_index": index, "reason": "fixture failure"})
    atomic_write_json(paths["manifest"], {
        "schema_version": 4, "run_id": "fixture-run", "model_key": "fixture-model",
        "task": task, "structure_index": index,
        "input_structure_sha256": input_hash,
        "status": status, "attempt_count": 1,
        "lifecycle": {
            "calculator_created": True, "calculator_fresh": True,
            "calculator_released": True, "cleanup_completed": True,
            "mutable_state_reused": False,
        },
        "error": file_evidence(paths["error"], root) if status != "completed" else None,
        "outputs": outputs, "created_at": "2026-08-18T00:00:00+00:00",
        "updated_at": "2026-08-18T00:00:01+00:00",
    })


def run_tests(root: Path) -> None:
    write_frame(root, "single_point", 0)
    write_frame(root, "single_point", 1, "failed")
    write_frame(root, "relaxation", 0)
    write_frame(root, "relaxation", 1, "failed")
    for task, index in (("single_point", 0), ("single_point", 1), ("relaxation", 0), ("relaxation", 1)):
        result = validate_frame_artifact(
            root, "fixture-model", task, index,
            expected_run_id="fixture-run", expected_frame_count=2,
        )
        assert result["valid"], result

    missing_cleanup = frame_paths(root, "fixture-model", "single_point", 0)["cleanup"]
    missing_cleanup.unlink()
    result = validate_frame_artifact(root, "fixture-model", "single_point", 0)
    assert not result["valid"] and any("cleanup" in item for item in result["errors"])
    write_frame(root, "single_point", 0)
    bad_hash = frame_paths(root, "fixture-model", "single_point", 1)["input_structure"]
    bad_hash.write_text("tampered\n", encoding="utf-8")
    result = validate_frame_artifact(root, "fixture-model", "single_point", 1)
    assert not result["valid"] and any("hash" in item for item in result["errors"])
    print("frame artifact self-test passed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-root", type=Path)
    args = parser.parse_args()
    if args.fixture_root:
        run_tests(args.fixture_root.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="frame-artifact-") as temporary:
            run_tests(Path(temporary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
