"""Synthetic selection fixtures only; never use as real official-source evidence."""
from copy import deepcopy
import hashlib
from pathlib import Path

from common import canonical_json_sha256, sha256_file
from catalysis_selection import COMPONENTS


def fields(version="v1", checkpoint="fixture", parameters=None):
    variant = {"checkpoint_id": checkpoint, "parameters": parameters or {"domain": "surface"}, "head": None}
    text = "SYNTHETIC TEST FIXTURE. The surface configuration returns compatible total energies for all components."
    commit = "a" * 40
    return {
        "execution_variant": variant, "leaderboard_identity": None,
        "catalysis_assessment": {
            "official_repository": "https://github.com/fixture/model",
            "repository_verification": {"authority_url": "https://fixture.invalid", "reason": "synthetic ownership fixture", "verified_by": "test"},
            "commit_sha": commit, "checked_version": version,
            "search_scope": ["README.md", "examples", "calculator implementation"],
            "scope": {"system": "synthetic slab", "adsorbate": "NO3", "elements": ["N", "O", "Cu"],
                      "requested_tasks": ["single_point", "relaxation"], "dft_reference": "synthetic consistent eV total energies"},
            "sources": [{"id": "official", "kind": "official_github", "url": f"https://github.com/fixture/model/blob/{commit}/README.md",
                         "commit_sha": commit, "retrieved_at": "2026-09-10T00:00:00+00:00", "content": text,
                         "sha256": hashlib.sha256(text.encode()).hexdigest()}],
            "finding": "specialized_found", "unresolved_conflicts": [], "recommended_candidate_id": "surface",
            "candidates": [{"id": "surface", "execution_variant": deepcopy(variant), "source_ids": ["official"],
                            "applicability": "synthetic adsorption scope", "limitations": "fixture only", "compatibility": "compatible",
                            "energy_semantics": {"kind": "total_energy", "unit": "eV", "components_compatible": True,
                                                 "dft_level": "fixture", "reference_state": "consistent components", "rationale": "fixture documentation"}}],
            "decision": {"selected_candidate_id": "surface", "user_requested_variant": None, "reason": "choose compatible surface configuration",
                         "displayed_differences": "task selector compared with general configuration", "confirmed": True,
                         "confirmed_by": "user", "confirmed_at": "2026-09-10T00:00:00+00:00"},
        },
    }


def model(key="fixture-model", version="v1", checkpoint="fixture"):
    return {"model_key": key, "model_name": key, "version_or_variant": version, "snapshot_rank": None,
            **fields(version, checkpoint)}


def observation(root: Path, identity):
    key = identity["model_key"]
    path = root / "models" / key / "checkpoints" / "fixture.pt"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(b"fixture checkpoint")
    variant = identity["execution_variant"]
    return {"execution_variant": deepcopy(variant), "calculator_initializations": [
        {"component": component, "execution_variant": deepcopy(variant), "calculator_class": "fixture.Calculator",
         "object_id": str(index + 1), "inspection_method": "fixture attribute inspection",
         "inspection": deepcopy(variant), "checkpoint_path": path.relative_to(root).as_posix(), "checkpoint_sha256": sha256_file(path)}
        for index, component in enumerate(COMPONENTS)]}


def plan_binding(identity):
    return {"execution_variant": deepcopy(identity["execution_variant"]),
            "catalysis_assessment_sha256": canonical_json_sha256(identity["catalysis_assessment"])}
