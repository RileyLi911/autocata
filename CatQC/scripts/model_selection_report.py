"""Extract immutable report evidence and validate an agent-authored English report.

Standard library only. Does not run inference or alter the benchmark metrics.
Numerical prose claims use Markdown links to JSON pointers; qualitative claims
need a separate, hash-bound review. A generated draft is never a reviewed report.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from common import atomic_write_json, load_json, metric_values, read_csv, sha256_file, as_bool, as_float
from rank_models import CATEGORIES, STATUS_OK, validate_settings, evaluate, RANKING_COLUMNS, RELIABILITY_COLUMNS, full_indices, sp_coverage

from catalysis_selection import validate_selection as validate_catalysis

EVIDENCE = "model_selection_report_evidence.json"
REPORT = "model_selection_report.md"
REVIEW = "model_selection_report_review.json"
HEADINGS = (
    "Executive Summary", "Benchmark Scope", "Recommendations by Use Case",
    "Model-by-Model Assessment", "Robustness and Limitations", "Evidence Tables",
)
USES = {
    "single_point_energy": ("Single-point adsorption energies", "single_point", "full_sp", "mae_eV"),
    "candidate_ordering": ("Candidate ordering and screening", "single_point", "full_sp", "spearman"),
    "structure_preoptimization": ("Structure preoptimization", "relaxation", "reliability", "rate"),
    "relaxed_adsorption_energy": ("Relaxed adsorption energies", "relaxation", "common_relax", "mae_eV"),
}
INPUTS = (
    "benchmark_config.json", "models_manifest.json",
    "summary/dft_reference.csv", "summary/benchmark_predictions_long.csv",
    "summary/benchmark_metrics_by_model.csv", "summary/benchmark_missing_or_failed_frames.csv",
    "summary/benchmark_merge_report.json", "summary/model_run_status.csv",
    "summary/benchmark_dissociation_records.csv", "summary/benchmark_metrics_relax_intact_only.csv",
    "summary/relaxed_structure_index.csv", "summary/reproducibility_notes.md",
    "summary/model_reliability_review.csv", "summary/ranking_config.json",
    "summary/ranking_audit.json", "summary/model_ranking.csv",
    "summary/methodology.txt",
)
REQUIRED_INPUTS = INPUTS[:8]
BEGIN, END = "<!-- BEGIN VERIFIED EVIDENCE -->", "<!-- END VERIFIED EVIDENCE -->"
LINK = re.compile(r"\[([^\]\n]+)\]\(model_selection_report_evidence\.json#(/[^)\s]*)\)")
NUMBER = re.compile(r"(?<![A-Za-z_])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def display(value):
    if value is None:
        return "not available"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return format(value, ".8g")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def pointer(data, path):
    value = data
    for part in unquote(path).lstrip("/").split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def cite(data, path):
    label = display(pointer(data, path)).replace("|", "&#124;").replace("]", "&#93;")
    return f"[{label}]({EVIDENCE}#{path})"


def hashes(root):
    # Optional inputs are fingerprinted as absent too: later creation invalidates evidence.
    return {name: sha256_file(root / name) if (root / name).is_file() else None for name in INPUTS}


def stats(rows):
    values = metric_values([float(r["model_adsorption_energy_eV"]) for r in rows],
                           [float(r["dft_adsorption_energy_eV"]) for r in rows])
    return {"n": len(rows), "structure_indices": [int(r["structure_index"]) for r in rows],
            **{key: value if value is None or math.isfinite(value) else None for key, value in values.items()}}


def fixed_ok(row):
    indices = row.get("fixed_atom_indices")
    if isinstance(indices, str):
        try:
            indices = json.loads(indices)
        except ValueError:
            return False
    maximum = as_float(row.get("fixed_atom_max_displacement_A"))
    tolerance = as_float(row.get("fixed_atom_displacement_tolerance_A"))
    return (isinstance(indices, list) and bool(indices)
            and maximum is not None and tolerance is not None
            and 0 <= maximum <= tolerance and math.isclose(tolerance, 0.05, abs_tol=1e-12)
            and as_bool(row.get("fixed_atom_displacement_violation")) is False)


def build_evidence(root: Path):
    for name in REQUIRED_INPUTS:
        if not (root / name).is_file():
            raise ValueError(f"missing report input: {name}; merge results and write final model statuses first")
    config = load_json(root / "benchmark_config.json")
    manifest = load_json(root / "models_manifest.json")
    identities = manifest.get("models", [])
    for identity in identities:
        selection_errors = validate_catalysis(identity, manifest.get("model_selection_mode"))
        if selection_errors:
            raise ValueError("; ".join(selection_errors))
    keys = [item["model_key"] for item in identities]
    if not keys or len(set(keys)) != len(keys):
        raise ValueError("model manifest must contain unique selected model identities")
    requested = config["benchmark_request"]["requested_tasks"]
    expected = int(config["frame_count"])
    if expected < 1 or not requested or set(requested) - {"single_point", "relaxation"}:
        raise ValueError("invalid expected frame count or requested tasks")
    sources = hashes(root)
    def optional_json(name):
        return load_json(root / name) if sources[name] else {}
    def optional_csv(name):
        return read_csv(root / name) if sources[name] else []
    analysis = optional_json("summary/ranking_config.json")
    audit = optional_json("summary/ranking_audit.json")
    if analysis:
        validate_settings(analysis)
    threshold = analysis.get("reliability_threshold", 0.95)
    excluded = set()
    statuses = read_csv(root / "summary/model_run_status.csv")
    if Counter(r["model_key"] for r in statuses) != Counter(keys):
        raise ValueError("model_run_status.csv must cover every selected model exactly once")
    state_by_key = {r["model_key"]: r for r in statuses}
    rows = read_csv(root / "summary/benchmark_predictions_long.csv")
    by_task = {task: {key: {} for key in keys} for task in ("SP", "Relax")}
    for row in rows:
        key, task, index = row["model_key"], row["task"], int(row["structure_index"])
        if key not in keys or task not in by_task or not 0 <= index < expected:
            raise ValueError("unknown model, task, or out-of-range structure in predictions")
        if {"SP": "single_point", "Relax": "relaxation"}[task] not in requested:
            raise ValueError("prediction row belongs to an unrequested task")
        if index in by_task[task][key]:
            raise ValueError(f"duplicate prediction row: {key}/{task}/{index}")
        by_task[task][key][index] = row
    valid = {task: {} for task in by_task}
    for task, models in by_task.items():
        for key, frames in models.items():
            valid[task][key] = {}
            for index, row in frames.items():
                usable = (row.get("status", "").lower() in STATUS_OK
                          and as_float(row.get("model_adsorption_energy_eV")) is not None
                          and as_float(row.get("dft_adsorption_energy_eV")) is not None)
                if task == "Relax":
                    if row.get("reliability_category") == "Intact and converged" and (
                            not usable or as_bool(row.get("converged")) is not True or not fixed_ok(row)):
                        raise ValueError(f"reliable Relax row lacks valid energy, convergence, or fixed-layer evidence: {key}/{index}")
                    usable = usable and as_bool(row.get("converged")) is True and fixed_ok(row)
                if usable:
                    valid[task][key][index] = row
    models = []
    for identity in identities:
        key = identity["model_key"]
        relax = by_task["Relax"][key]
        counts = {category: sum(r.get("reliability_category") == category for r in relax.values()) for category in CATEGORIES}
        rate = counts["Intact and converged"] / expected if "relaxation" in requested else None
        enabled = identity.get("enabled", True)
        decision = ("disabled" if not enabled else "not_applicable" if rate is None else
                    "excluded" if rate < threshold else "pass")
        if decision == "excluded":
            excluded.add(key)
        models.append({
            "identity": identity, "model_key": key, "model_name": identity.get("model_name", key),
            "run_status": state_by_key[key], "review_status": decision,
            "all_valid_sp": stats(list(valid["SP"][key].values())),
            "all_valid_relax": stats(list(valid["Relax"][key].values())),
            "reliability": {"rate": rate, "expected": expected if rate is not None else None,
                            "categories": counts, "missing_count": expected - len(relax) if rate is not None else None,
                            "unclassified_count": len(relax) - sum(counts.values()),
                            "fixed_layer_verified_count": sum(fixed_ok(r) for r in relax.values()),
                            "fixed_layer_missing_or_failed_count": sum(not fixed_ok(r) for r in relax.values())},
            "coverage": {"SP": len(valid["SP"][key]) / expected if "single_point" in requested else None,
                         "Relax": len(valid["Relax"][key]) / expected if rate is not None else None},
            "failures": [r for r in optional_csv("summary/benchmark_missing_or_failed_frames.csv") if r.get("model_key") == key],
        })
    active = [m["model_key"] for m in models if m["review_status"] not in {"disabled", "excluded"}]
    coverage = sp_coverage(root, config, keys, by_task["SP"])
    common = {}
    for task in ("SP", "Relax"):
        subsets = [{i for i, row in valid[task][key].items()
                    if task == "SP" or row.get("reliability_category") == "Intact and converged"} for key in active]
        common[task] = sorted(set.intersection(*subsets)) if subsets else []
    common["SP"] = full_indices(config) if "single_point" in requested else []
    for model in models:
        key = model["model_key"]
        model["sp_completion"] = coverage[key]
        if "single_point" in requested:
            model["coverage"]["SP"] = coverage[key]["fraction"]
        unavailable_indices = set(coverage[key]["missing_or_failed_indices"])
        model["all_valid_sp"] = stats([row for i, row in valid["SP"][key].items() if i not in unavailable_indices])
        for task, field in (("SP", "full_sp"), ("Relax", "common_relax")):
            model[field] = stats([valid[task][key][i] for i in common[task]]) if key in active and (task != "SP" or not coverage[key]["problems"]) else stats([])

    # Recompute the ranking and validate both provenance and all published values.
    available = False
    ranking_rows = []
    ranking_status = "missing"
    ranking_reason = "Run rank_models.py after recording the user's ranking settings."
    if audit:
        if not analysis:
            raise ValueError("ranking audit exists without ranking settings")
        expected_review, expected_ranking, expected_audit = evaluate(root, analysis)
        if {k: v for k, v in audit.items() if k != "output_sha256"} != expected_audit:
            raise ValueError("ranking audit is stale or altered; rerun ranking")
        for name in ("model_reliability_review.csv", "model_ranking.csv", "methodology.txt"):
            if sources["summary/" + name] is None or sources["summary/" + name] != (audit.get("output_sha256") or {}).get(name):
                raise ValueError(f"ranking output fingerprint mismatch: {name}")
        for filename, expected_rows, columns in (
                ("model_reliability_review.csv", expected_review, RELIABILITY_COLUMNS),
                ("model_ranking.csv", expected_ranking, RANKING_COLUMNS)):
            actual_rows = optional_csv("summary/" + filename)
            if [r["model_key"] for r in actual_rows] != [r["model_key"] for r in expected_rows]:
                raise ValueError(f"ranking rows/order mismatch: {filename}")
            for actual, expected_row in zip(actual_rows, expected_rows):
                for field in columns:
                    value = expected_row.get(field)
                    observed = actual.get(field)
                    if isinstance(value, (int, float)):
                        matches = as_float(observed) is not None and math.isclose(float(observed), value, rel_tol=0, abs_tol=1e-12)
                    else:
                        matches = observed == ("" if value is None else str(value))
                    if not matches:
                        raise ValueError(f"ranking metric drift: {actual['model_key']}/{field}")
        if audit["model_keys_included"] != active or audit["sp_structure_indices"] != common["SP"] or audit["relax_structure_indices"] != common["Relax"]:
            raise ValueError("ranking cohort or common sample mismatch")
        ranking_status, ranking_reason = audit["status"], audit["reason"]
        available = ranking_status == "complete"
        ranking_rows = expected_ranking
    for model in models:
        model["ranking"] = next((r for r in ranking_rows if r["model_key"] == model["model_key"]), None)
    uses = {}
    for name, (title, task, group, metric) in USES.items():
        usable = [m["model_key"] for m in models if m["model_key"] in active and m[group][metric] is not None
                  and (group != "reliability" or m["reliability"]["categories"]["Intact and converged"] > 0)]
        status = "not_evaluated" if task not in requested else "assessable" if usable else "insufficient_evidence"
        uses[name] = {"title": title, "status": status, "eligible_model_keys": usable if task in requested else [],
                      "basis": f"{group}.{metric}", "note": "Eligibility is not a recommendation or a ranking."}
    return {"schema_version": 3, "language": "en", "status": "ready_for_review",
            "sources": sources, "scope": config, "requested_tasks": requested,
            "comparison": {"included_model_keys": active, "excluded_model_keys": sorted(excluded),
                           "reliability_threshold": threshold, "full_dataset_size": expected,
                           "full_dataset_indices": full_indices(config), "sp_coverage": coverage,
                           "sp_structure_indices": common["SP"], "relax_structure_indices": common["Relax"],
                           "n_sp_full": len(common["SP"]), "n_relax_common": len(common["Relax"]),
                           "ranking_available": available, "ranking_status": ranking_status,
                           "ranking_unavailable_reason": ranking_reason, "ranking_config": analysis,
                           "ranking_audit": audit or None},
            "use_cases": uses, "models": models,
            "definitions": {"full_sp": "All frozen input indices with valid SP predictions and DFT references; unavailable for incomplete models. Partial valid metrics are diagnostic only.",
                            "common_relax": "Intersection of effective valid Intact and converged Relax rows across included enabled models, matched to DFT.",
                            "all_valid_sp": "Diagnostic valid SP subset; never a full-dataset metric when coverage is incomplete.",
                            "all_valid_relax": "All numerically valid converged Relax rows, including dissociation; not the common reliable subset.",
                            "reliability": "Intact and converged divided by the full expected Relax frame count; missing frames remain in the denominator.",
                            "limitations": "Dataset-specific relative evidence only; no speed, memory, cost, dynamics, barrier, or DFT-minimum proximity claims."}}


def evidence_block(data):
    lines = [BEGIN, "## Evidence Tables", "", f"Report status: {data['status']}.",
             f"Full SP dataset size: {cite(data, '/comparison/n_sp_full')}; common reliable Relax sample count: {cite(data, '/comparison/n_relax_common')}.",
             "", "| Model | Review | SP MAE (eV), full dataset | SP Spearman, full dataset | Relax MAE (eV), common reliable | Relax reliability | SP coverage | Relax coverage |",
             "|---|---|---|---|---|---|---|---|"]
    for i, model in enumerate(data["models"]):
        base = f"/models/{i}"
        fields = ("model_name", "review_status", "full_sp/mae_eV", "full_sp/spearman", "common_relax/mae_eV", "reliability/rate", "coverage/SP", "coverage/Relax")
        lines.append("| " + " | ".join(cite(data, f"{base}/{field}") for field in fields) + " |")
    lines += ["", "### Final weighted ranking", "",
              f"Ranking status: {cite(data, '/comparison/ranking_status')}.",
              f"Ranking settings: {cite(data, '/comparison/ranking_config')}.",
              f"Unavailable reason: {cite(data, '/comparison/ranking_unavailable_reason')}.",
              f"SP coverage and missing/failed indices: {cite(data, '/comparison/sp_coverage')}.", "",
              "| Model | Rank | Total score | Relax MAE contribution | SP MAE contribution | Spearman contribution |",
              "|---|---|---|---|---|---|"]
    ordered = sorted(((i, m) for i, m in enumerate(data["models"]) if m["ranking"]),
                     key=lambda pair: (pair[1]["ranking"]["rank"], pair[1]["model_name"], pair[1]["model_key"]))
    for i, model in ordered:
        paths = [f"/models/{i}/model_name"] + [f"/models/{i}/ranking/{field}" for field in
            ("rank", "total_score", "contribution_relax_mae", "contribution_sp_mae", "contribution_spearman")]
        lines.append("| " + " | ".join(cite(data, path) for path in paths) + " |")
    lines += ["", "### Configuration selection (official evidence, not measured superiority)", "",
              "| Model | Actual checkpoint | Task parameters | Head | Selection rationale | Original leaderboard identity | Official GitHub evidence |",
              "|---|---|---|---|---|---|---|"]
    for i, model in enumerate(data["models"]):
        paths = [f"/models/{i}/model_name"] + [f"/models/{i}/identity/{field}" for field in
            ("execution_variant/checkpoint_id", "execution_variant/parameters", "execution_variant/head",
             "catalysis_assessment/decision/reason", "leaderboard_identity")]
        assessment = model["identity"]["catalysis_assessment"]
        candidate = next(c for c in assessment["candidates"] if c["id"] == assessment["decision"]["selected_candidate_id"])
        official = next(source for source in assessment["sources"] if source["kind"] == "official_github" and source["id"] in candidate["source_ids"])
        lines.append("| " + " | ".join(cite(data, path) for path in paths) + f" | [Pinned source]({official['url']}) |")
    lines += ["", "Source files (paths relative to this report):", ""]
    for name, digest in data["sources"].items():
        if digest:
            relative = name.removeprefix("summary/") if name.startswith("summary/") else "../" + name
            lines.append(f"- [{name}]({relative}) — SHA-256 `{digest}`")
    lines += ["", "See the evidence JSON for model versions, failure records, category counts, fixed-layer checks, per-model and common-sample error statistics, and verified weighted ranking evidence.", END]
    return "\n".join(lines)


def draft(data):
    lines = ["# Model Selection Report", "", "## Executive Summary", "", "TODO: State dataset-specific recommendations and the principal limitations.", "",
             "| Use case | Candidate models | Evidence | Conditions and limitations |", "|---|---|---|---|",
             *[f"| {value['title']} | TODO | {value['status']} | TODO |" for value in data["use_cases"].values()],
             "", "## Benchmark Scope", "", "TODO: Describe the dataset, adsorbate, model versions, requested tasks, DFT reference semantics, coverage and protocol exceptions.",
             "", "## Recommendations by Use Case", ""]
    for name, use in data["use_cases"].items():
        lines += [f"### {use['title']}", f"<!-- use-case:{name} -->", f"Assessment status: {use['status']}.",
                  "TODO: Candidates, evidence, tradeoffs and conditions. Use 'Not evaluated' or 'Insufficient evidence' when applicable.", ""]
    lines += ["## Model-by-Model Assessment", ""]
    for i, model in enumerate(data["models"]):
        lines += [f"### {model['model_name']}", f"<!-- model:{model['model_key']} -->",
                  f"Evidence: {cite(data, f'/models/{i}/review_status')}.",
                  f"Configuration choice: {cite(data, f'/models/{i}/identity/catalysis_assessment/decision/reason')}. Official applicability is not measured superiority.",
                  "Strengths: TODO", "Limitations: TODO", "Suitable work and conditions: TODO", ""]
    lines += ["## Robustness and Limitations", "",
              "TODO: Explain common samples, missing data, reliability exclusions, normalization cohort, configured weights and ranking limitations. Do not infer absolute suitability from a relative lead.",
              "", evidence_block(data), ""]
    return "\n".join(lines)


def validate_report(root: Path, require_review=True, final=False):
    errors = []
    summary = root / "summary"
    try:
        data = load_json(summary / EVIDENCE)
        current = build_evidence(root)
        if data != current:
            errors.append("report evidence is stale or altered; rebuild evidence and re-review the report")
        text = (summary / REPORT).read_text(encoding="utf-8")
        block = evidence_block(data)
        if text.count(BEGIN) != 1 or text.count(END) != 1 or block not in text:
            errors.append("verified evidence table is missing or modified")
        for title in HEADINGS:
            if text.count(f"## {title}\n") != 1:
                errors.append(f"missing or duplicate report section: {title}")
        if re.search(r"\b(?:TODO|TBD|FIXME)\b", text):
            errors.append("report still contains draft placeholders")
        for name, use in data["use_cases"].items():
            marker = f"<!-- use-case:{name} -->"
            if text.count(marker) != 1:
                errors.append(f"missing or duplicate use-case assessment: {name}")
                continue
            segment = text.split(marker, 1)[1].split("\n#", 1)[0]
            if f"Assessment status: {use['status']}." not in segment:
                errors.append(f"incorrect use-case status: {name}")
            if use["status"] == "assessable" and not LINK.search(segment):
                errors.append(f"use-case recommendation lacks evidence citation: {name}")
        found = re.findall(r"<!-- model:([^>]+) -->", text)
        if Counter(found) != Counter(m["model_key"] for m in data["models"]):
            errors.append("report must assess every selected model exactly once")
        for i, model in enumerate(data["models"]):
            marker = f"<!-- model:{model['model_key']} -->"
            if marker not in text:
                continue
            segment = text.split(marker, 1)[1].split("\n#", 1)[0]
            for label in ("Configuration choice:", "Strengths:", "Limitations:", "Suitable work and conditions:"):
                if not re.search(re.escape(label) + r"\s*\S[^\n]*", segment):
                    errors.append(f"missing model assessment field: {model['model_key']}/{label}")
            if f"{EVIDENCE}#/models/{i}/" not in segment:
                errors.append(f"model assessment lacks its own evidence citation: {model['model_key']}")
        prose = text.replace(block, "")
        for match in LINK.finditer(prose):
            try:
                expected_label = display(pointer(data, match[2])).replace("|", "&#124;").replace("]", "&#93;")
                if match[1] != expected_label:
                    errors.append(f"incorrect evidence value: {match[2]}")
            except (ValueError, KeyError, IndexError, TypeError):
                errors.append(f"invalid evidence pointer: {match[2]}")
        # Only verified links may carry quantitative claims in editable prose.
        remainder = LINK.sub("", prose)
        remainder = re.sub(r"<!--.*?-->", "", remainder, flags=re.S)
        remainder = re.sub(r"\]\([^)]*\)", "]", remainder)
        for model in data["models"]:
            for literal in sorted({model["model_name"], model["model_key"]}, key=len, reverse=True):
                remainder = remainder.replace(literal, "MODEL")
        if NUMBER.search(remainder):
            errors.append("unbound numerical claim in report prose; use an evidence JSON-pointer link (including version/formula/count values)")
        if re.search(r"[\u4e00-\u9fff]", remainder):
            errors.append("report narrative must be English")
        if final and set(data["requested_tasks"]) == {"single_point", "relaxation"} and data["comparison"]["ranking_status"] not in {"complete", "unavailable"}:
            errors.append("final report requires a fresh ranking audit; pending_sp_completion must be resolved before completion")
        if require_review:
            review = load_json(summary / REVIEW)
            expected_checks = [*USES, *[f"model:{m['model_key']}" for m in data["models"]], "scope", "numerical_claims", "limitations"]
            if (not review.get("reviewed_by") or not review.get("reviewed_at")
                    or review.get("checked_items") != expected_checks
                    or review.get("report_sha256") != sha256_file(summary / REPORT)
                    or review.get("evidence_sha256") != sha256_file(summary / EVIDENCE)):
                errors.append("missing or stale semantic review; review all model and use-case claims again")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(f"report validation: {exc}")
    return {"passed": not errors, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build-evidence", "draft", "validate", "record-review"))
    parser.add_argument("benchmark_root", type=Path)
    parser.add_argument("--reviewed-by")
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    root = args.benchmark_root.resolve()
    summary = root / "summary"
    try:
        if args.command == "build-evidence":
            data = build_evidence(root)
            atomic_write_json(summary / EVIDENCE, data)
            result = {"passed": True, "status": data["status"], "evidence": str(summary / EVIDENCE)}
        elif args.command == "draft":
            data = load_json(summary / EVIDENCE)
            if data != build_evidence(root):
                raise ValueError("rebuild stale evidence before drafting")
            with (summary / REPORT).open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(draft(data))
            result = {"passed": True, "draft_only": True, "report": str(summary / REPORT)}
        elif args.command == "record-review":
            if not args.reviewed_by or not args.reviewed_by.strip():
                raise ValueError("--reviewed-by is required after actual semantic review")
            result = validate_report(root, require_review=False, final=args.final)
            if result["passed"]:
                data = load_json(summary / EVIDENCE)
                atomic_write_json(summary / REVIEW, {
                    "schema_version": 1, "reviewed_by": args.reviewed_by,
                    "reviewed_at": datetime.now(timezone.utc).isoformat(),
                    "report_sha256": sha256_file(summary / REPORT),
                    "evidence_sha256": sha256_file(summary / EVIDENCE),
                    "checked_items": [*USES, *[f"model:{m['model_key']}" for m in data["models"]], "scope", "numerical_claims", "limitations"],
                })
        else:
            result = validate_report(root, final=args.final)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
