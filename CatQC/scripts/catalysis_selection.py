"""Version-bound catalysis selection and observed calculator configuration gates.

Official ownership and scientific applicability need an agent's source review;
these deterministic checks enforce its archived evidence and selected identity.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from common import canonical_json_sha256, load_json, sha256_file

COMPONENTS = ("slab_plus_adsorbate", "clean_slab", "isolated_adsorbate")
FIELDS = ("catalysis_assessment", "execution_variant", "leaderboard_identity")
LEGACY = "missing catalysis selection evidence; resume with the original skill or confirm a new run (no silent migration)"


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def variant_errors(value):
    if not isinstance(value, dict):
        return ["execution_variant must be an object"]
    errors = []
    if set(value) != {"checkpoint_id", "parameters", "head"}:
        errors.append("execution_variant requires exactly checkpoint_id, parameters and head")
    if not nonempty(value.get("checkpoint_id")):
        errors.append("checkpoint_id must identify an exact checkpoint/version")
    params = value.get("parameters")
    if not isinstance(params, dict) or any(not nonempty(k) for k in params):
        errors.append("parameters must be a generic string-keyed object (empty only when no selector exists)")
    try:
        json.dumps(params, allow_nan=False)
    except (ValueError, TypeError):
        errors.append("parameters must contain finite JSON values")
    if value.get("head") is not None and not nonempty(value.get("head")):
        errors.append("head must be an explicit name or null")
    return errors


def validate_selection(model, mode=None):
    try:
        return _validate_selection(model, mode)
    except (TypeError, AttributeError, KeyError, ValueError) as exc:
        return [f"malformed catalysis selection: {exc}"]


def _validate_selection(model, mode=None):
    errors = []
    if any(field not in model for field in FIELDS):
        return [LEGACY]
    variant = model["execution_variant"]
    errors.extend(variant_errors(variant))
    a = model["catalysis_assessment"]
    if not isinstance(a, dict):
        return errors + ["catalysis_assessment must be an object"]
    for field in ("official_repository", "repository_verification", "commit_sha", "checked_version", "search_scope", "scope", "sources", "candidates", "finding", "recommended_candidate_id", "decision", "unresolved_conflicts"):
        if field not in a:
            errors.append(f"catalysis_assessment missing {field}")
    repository = str(a.get("official_repository") or "").rstrip("/")
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        errors.append("official_repository must be an exact official GitHub repository URL")
    verification = a.get("repository_verification") or {}
    if not all(nonempty(verification.get(f)) for f in ("authority_url", "reason", "verified_by")):
        errors.append("repository ownership requires authority_url, reason and verified_by")
    commit = a.get("commit_sha")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        errors.append("official sources must be pinned to a full commit SHA")
    if not nonempty(a.get("checked_version")) or a.get("checked_version") != model.get("version_or_variant"):
        errors.append("checked_version must match the actual model version_or_variant")
    if not isinstance(a.get("search_scope"), list) or not a["search_scope"] or not all(nonempty(x) for x in a["search_scope"]):
        errors.append("search_scope must record the examined docs/examples/config/code locations")
    scope = a.get("scope") or {}
    for field in ("system", "adsorbate", "elements", "requested_tasks", "dft_reference"):
        if not scope.get(field):
            errors.append(f"selection applicability scope missing {field}")
    if not isinstance(scope.get("requested_tasks"), list) or set(scope.get("requested_tasks", [])) - {"single_point", "relaxation"}:
        errors.append("scope requested_tasks must use the benchmark task names")
    sources = a.get("sources") or []
    if not isinstance(sources, list) or not sources:
        return errors + ["saved official GitHub source content is required"]
    source_ids, github_count = set(), 0
    for source in sources:
        if not isinstance(source, dict):
            errors.append("source must be an object")
            continue
        sid = source.get("id")
        if not nonempty(sid) or sid in source_ids:
            errors.append("source ids must be nonempty and unique")
        source_ids.add(sid)
        content = source.get("content")
        if not nonempty(content) or hashlib.sha256(str(content).encode("utf-8")).hexdigest() != source.get("sha256"):
            errors.append(f"saved source content hash mismatch: {sid}")
        if not nonempty(source.get("retrieved_at")):
            errors.append(f"source retrieval timestamp missing: {sid}")
        url = str(source.get("url") or "")
        if source.get("kind") == "official_github":
            github_count += 1
            if not url.startswith(repository + "/blob/" + str(commit) + "/") or source.get("commit_sha") != commit:
                errors.append(f"GitHub source must belong to the verified repository and pinned commit: {sid}")
        elif source.get("kind") not in {"official_model_card", "primary_paper", "official_docs"} or urlparse(url).scheme != "https":
            errors.append(f"invalid supplemental source: {sid}")
    if not github_count:
        errors.append("at least one primary official GitHub source is required; supplemental evidence cannot replace it")
    if a.get("finding") not in {"specialized_found", "no_specialized_found_in_checked_sources"}:
        errors.append("insufficient or unresolved catalysis evidence cannot be frozen")
    if a.get("unresolved_conflicts") != []:
        errors.append("unresolved documentation/code/version conflicts block selection")
    candidates = a.get("candidates") or []
    if not isinstance(candidates, list) or not candidates:
        return errors + ["candidate comparison is required even when retaining the general configuration"]
    by_id = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            errors.append("candidate must be an object")
            continue
        cid = candidate.get("id")
        if not nonempty(cid) or cid in by_id:
            errors.append("candidate ids must be unique")
        by_id[cid] = candidate
        errors.extend(variant_errors(candidate.get("execution_variant")))
        if not nonempty(candidate.get("applicability")) or not nonempty(candidate.get("limitations")):
            errors.append(f"candidate requires applicability and limitations: {cid}")
        refs = candidate.get("source_ids") or []
        if not refs or any(x not in source_ids for x in refs):
            errors.append(f"candidate has missing/unknown source references: {cid}")
        if not any(source.get("id") in refs and source.get("kind") == "official_github" for source in sources):
            errors.append(f"candidate needs primary official GitHub evidence: {cid}")
        if candidate.get("compatibility") not in {"compatible", "incompatible", "unresolved"}:
            errors.append(f"candidate compatibility missing: {cid}")
        energy = candidate.get("energy_semantics") or {}
        for field in ("kind", "unit", "dft_level", "reference_state", "rationale"):
            if not nonempty(energy.get(field)):
                errors.append(f"candidate energy semantics missing {field}: {cid}")
    decision = a.get("decision") or {}
    chosen = by_id.get(decision.get("selected_candidate_id"))
    recommended = a.get("recommended_candidate_id")
    if recommended not in by_id:
        errors.append("recommended candidate must be part of the comparison")
    for field in ("reason", "displayed_differences", "confirmed_at"):
        if not nonempty(decision.get(field)):
            errors.append(f"user decision missing {field}")
    if decision.get("confirmed_by") != "user" or decision.get("confirmed") is not True:
        errors.append("model and execution configuration require combined explicit user confirmation")
    try:
        datetime.fromisoformat(str(decision.get("confirmed_at")))
    except ValueError:
        errors.append("decision confirmed_at must be ISO-8601")
    if "user_requested_variant" not in decision:
        errors.append("preserve user_requested_variant (null when unspecified)")
    elif decision["user_requested_variant"] is not None:
        errors.extend(variant_errors(decision["user_requested_variant"]))
    if not chosen or chosen.get("execution_variant") != variant:
        errors.append("execution_variant must equal the user-selected candidate")
    elif chosen.get("compatibility") != "compatible":
        errors.append("selected candidate is incompatible or unresolved")
    else:
        energy = chosen.get("energy_semantics") or {}
        if energy.get("kind") != "total_energy" or energy.get("unit") != "eV" or energy.get("components_compatible") is not True:
            errors.append("selected energy output cannot use the frozen three-total-energy adsorption formula; resolve the scientific definition first")
    leaderboard = model.get("leaderboard_identity")
    if mode == "matbench_top_n" or model.get("snapshot_rank") is not None:
        if not isinstance(leaderboard, dict) or any(not leaderboard.get(f) for f in ("model_name", "version_or_variant", "source_url", "snapshot_rank")):
            errors.append("Matbench selection must preserve original leaderboard identity")
        else:
            errors.extend(variant_errors(leaderboard.get("execution_variant")))
            if leaderboard["snapshot_rank"] != model.get("snapshot_rank"):
                errors.append("leaderboard snapshot rank mismatch")
    elif leaderboard is not None:
        errors.append("named selection must have leaderboard_identity=null")
    return errors


def frozen_model(model_root):
    root = model_root.resolve().parents[1]
    manifest = load_json(root / "models_manifest.json")
    if (root / "run_manifest.json").is_file():
        run = load_json(root / "run_manifest.json")
        if run.get("models_manifest_sha256") != canonical_json_sha256(manifest):
            raise ValueError("frozen models manifest hash differs from run identity")
    matches = [m for m in manifest.get("models", []) if m.get("model_key") == model_root.name]
    if len(matches) != 1:
        raise ValueError("model is absent or duplicated in the frozen manifest")
    errors = validate_selection(matches[0], manifest.get("model_selection_mode"))
    if errors:
        raise ValueError("; ".join(errors))
    return matches[0]


def binding_errors(model_root, plan):
    try:
        model = frozen_model(model_root)
    except (OSError, ValueError, KeyError) as exc:
        return [str(exc)]
    errors = []
    if plan.get("execution_variant") != model["execution_variant"]:
        errors.append("usage plan execution_variant differs from confirmed selection")
    if plan.get("catalysis_assessment_sha256") != canonical_json_sha256(model["catalysis_assessment"]):
        errors.append("usage plan catalysis assessment hash mismatch")
    if (plan.get("checkpoint_loading") or {}).get("variant") != model["execution_variant"]["checkpoint_id"]:
        errors.append("usage plan checkpoint variant differs from selected checkpoint_id")
    root = model_root.resolve().parents[1]
    recorded_path = (plan.get("checkpoint_loading") or {}).get("path")
    try:
        if not recorded_path:
            raise ValueError("missing path")
        (root / recorded_path).resolve().relative_to((model_root / "checkpoints").resolve())
    except ValueError:
        errors.append("usage plan requires the exact model-local checkpoint path")
    prep_path = model_root / "environment_preparation.json"
    prep = load_json(prep_path) if prep_path.is_file() else {}
    selected_sources = {(source["sha256"], source["url"]) for source in model["catalysis_assessment"]["sources"] if source["kind"] == "official_github"}
    aligned = [guide for guide in prep.get("official_guides", [])
               if guide.get("artifact_path") in plan.get("source_guide_paths", [])
               and guide.get("scope") in {"usage", "both"}
               and guide.get("revision") == model["catalysis_assessment"]["commit_sha"]
               and (guide.get("content_sha256"), guide.get("source_url")) in selected_sources]
    if not aligned:
        errors.append("usage guide is not bound to the selected official GitHub content and commit")
    return errors


def observation_errors(record, model, root=None, all_components=True):
    """Check observed calculator records, not just an intended task_name/pass flag."""
    errors = []
    expected = model.get("execution_variant")
    if variant_errors(expected):
        return [LEGACY]
    if record.get("execution_variant") != expected:
        errors.append("observed execution_variant differs from confirmed selection")
    observations = record.get("calculator_initializations")
    if not isinstance(observations, list) or not observations:
        return errors + ["missing actual calculator initialization observations"]
    seen = set()
    for observed in observations:
        if not isinstance(observed, dict):
            errors.append("calculator observation must be an object")
            continue
        component = observed.get("component")
        if component not in COMPONENTS:
            errors.append("unknown energy component in calculator observation")
        seen.add(component)
        if observed.get("execution_variant") != expected:
            errors.append(f"calculator component configuration mismatch: {component}")
        if not nonempty(observed.get("calculator_class")) or not nonempty(str(observed.get("object_id") or "")):
            errors.append(f"calculator object identity missing: {component}")
        inspection = observed.get("inspection")
        if not isinstance(inspection, dict) or not inspection or not nonempty(observed.get("inspection_method")):
            errors.append(f"provider attribute/config inspection missing: {component}")
        elif any(field not in inspection or inspection.get(field) != expected[field] for field in ("checkpoint_id", "parameters", "head")):
            errors.append(f"inspected provider configuration differs from selected variant: {component}")
        if root is not None:
            path = root / str(observed.get("checkpoint_path") or "")
            plan_path = root / "models" / model["model_key"] / "resources" / "model_usage_plan.json"
            if plan_path.is_file():
                configured = (load_json(plan_path).get("checkpoint_loading") or {}).get("path")
                if not configured or path.resolve() != (root / configured).resolve():
                    errors.append(f"component checkpoint differs from the frozen usage plan: {component}")
            try:
                path.resolve().relative_to((root / "models" / model["model_key"] / "checkpoints").resolve())
            except ValueError:
                errors.append(f"observed checkpoint outside model checkpoint directory: {component}")
            else:
                if not path.is_file() or sha256_file(path) != observed.get("checkpoint_sha256"):
                    errors.append(f"observed checkpoint missing or changed: {component}")
    if all_components and seen != set(COMPONENTS):
        errors.append("all three energy components require configuration observations")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("selection_file", type=Path)
    args = parser.parse_args()
    try:
        payload = load_json(args.selection_file)
        models = payload.get("models") or []
        errors = [f"model {i}: {e}" for i, m in enumerate(models) for e in validate_selection(m, payload.get("model_selection_mode"))]
        if not models:
            errors.append("selection must contain models")
    except (ValueError, TypeError, OSError, KeyError) as exc:
        errors = [str(exc)]
    print(json.dumps({"passed": not errors, "errors": errors}, indent=2))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
