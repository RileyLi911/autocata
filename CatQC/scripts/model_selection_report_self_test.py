"""Report regression fixtures: no models, downloads or scientific execution."""
from __future__ import annotations

import contextlib
import csv
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from common import atomic_write_json, load_json
import model_selection_report as report
from catalysis_test_support import fields as catalysis_fields
from rank_models import main as ranking_main


def csv_file(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row)) or ["model_key"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fields)
        writer.writeheader()
        writer.writerows(rows)


def fixture(root, tasks=("single_point", "relaxation"), bad=False, constant=False, disjoint=False):
    root.mkdir(parents=True)
    summary = root / "summary"
    summary.mkdir()
    atomic_write_json(root / "benchmark_config.json", {
        "frame_count": 3, "structure_indices": [0, 1, 2], "benchmark_request": {"requested_tasks": list(tasks), "dataset_name": "fixture", "adsorbate": "NO3"},
        "energy_unit": "eV", "scientific_exceptions": [],
    })
    atomic_write_json(root / "models_manifest.json", {"models": [
        {"model_key": key, "model_name": key.upper(), "version_or_variant": "test-v1", "enabled": True, **catalysis_fields("test-v1")} for key in ("a", "b")
    ]})
    rows = []
    for key in ("a", "b"):
        for task in tasks:
            for i in range(3):
                if disjoint and task == "single_point" and ((key == "a" and i != 0) or (key == "b" and i == 0)):
                    continue
                category = "Detached" if bad and key == "b" and i == 2 else "Intact and converged"
                rows.append({"task": "SP" if task == "single_point" else "Relax", "model_key": key,
                             "structure_index": i, "slab_composition": "Cu", "status": "success", "converged": True,
                             "dft_adsorption_energy_eV": -i - 1,
                             "model_adsorption_energy_eV": -1 if constant else -i - 1 + (0.1 if key == "a" else 0.2),
                             "reliability_category": category if task == "relaxation" else "not_applicable",
                             "fixed_atom_indices": "[0]", "fixed_atom_max_displacement_A": 0,
                             "fixed_atom_displacement_tolerance_A": 0.05, "fixed_atom_displacement_violation": False})
    csv_file(summary / "benchmark_predictions_long.csv", rows)
    csv_file(summary / "model_run_status.csv", [{"model_key": k, "final_status": "complete", "notes": "fixture"} for k in ("a", "b")])
    csv_file(summary / "benchmark_metrics_by_model.csv", [])
    csv_file(summary / "benchmark_missing_or_failed_frames.csv", [])
    csv_file(summary / "dft_reference.csv", [{"structure_index": i, "dft_adsorption_energy_eV": -i - 1} for i in range(3)])
    atomic_write_json(summary / "benchmark_merge_report.json", {"passed": True})
    return root


def invoke(command, root, *args):
    with patch.object(sys, "argv", ["model_selection_report.py", command, str(root), *args]), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return report.main()


def ranking(root, *args):
    with patch.object(sys, "argv", ["rank_models.py", str(root), "--accept-defaults", *args]), contextlib.redirect_stdout(io.StringIO()):
        return ranking_main()


def write_reviewable(root):
    """Synthetic authored text for validator tests, not a production recommendation."""
    if not (root / "summary/ranking_audit.json").exists():
        assert ranking(root) == 0
    assert invoke("build-evidence", root) == 0
    data = load_json(root / "summary" / report.EVIDENCE)
    text = report.draft(data)
    # Real runs require an agent to replace these fields using actual evidence.
    text = text.replace("TODO", "Evidence is limited to this fixture; no universal suitability claim.")
    for name, use in data["use_cases"].items():
        marker = f"<!-- use-case:{name} -->"
        text = text.replace(marker, marker + "\nEvidence: " + report.cite(data, "/models/0/full_sp/mae_eV"))
    (root / "summary" / report.REPORT).write_text(text, encoding="utf-8")
    assert invoke("record-review", root, "--reviewed-by", "fixture-reviewer") == 0
    return data, text


def fails(root, fragment, **kwargs):
    result = report.validate_report(root, **kwargs)
    assert not result["passed"], result
    assert any(fragment in item for item in result["errors"]), result


def main():
    checks = 0
    with tempfile.TemporaryDirectory(prefix="selection-report-test-") as directory:
        base = Path(directory)
        root = fixture(base / "full")
        assert ranking(root) == 0
        data, original = write_reviewable(root)
        assert data["comparison"]["ranking_available"]
        assert data["comparison"]["n_sp_full"] == 3
        assert abs(data["models"][0]["full_sp"]["mae_eV"] - 0.1) < 1e-12
        assert report.validate_report(root, final=True)["passed"]
        assert invoke("draft", root) == 1  # never overwrite authored prose
        checks += 1

        path = root / "summary" / report.REPORT
        path.write_text(original.replace("[0.1](model_selection_report_evidence.json#/models/0/full_sp/mae_eV)", "[9](model_selection_report_evidence.json#/models/0/full_sp/mae_eV)", 1), encoding="utf-8")
        fails(root, "incorrect evidence value")
        path.write_text(original + "\nMAE is 9 eV.\n", encoding="utf-8")
        fails(root, "unbound numerical")
        path.write_text(original.replace("<!-- model:b -->", ""), encoding="utf-8")
        fails(root, "every selected model")
        path.write_text(original + "\nAdditional qualitative limitation.\n", encoding="utf-8")
        fails(root, "stale semantic review")
        path.write_text(original, encoding="utf-8")
        checks += 4

        p = root / "summary/benchmark_metrics_by_model.csv"
        saved = p.read_bytes()
        p.write_bytes(saved + b"\n")
        fails(root, "stale or altered")
        p.write_bytes(saved)
        p = root / "summary/model_ranking.csv"
        saved = p.read_bytes()
        p.write_bytes(saved + b"\n")
        fails(root, "fingerprint mismatch")
        p.write_bytes(saved)
        checks += 2

        # Missing required output cannot be treated as optional uncertainty.
        p.unlink()
        fails(root, "fingerprint mismatch")
        p.write_bytes(saved)
        # Legacy files are deliberately ignored, even if malformed.
        (root / "summary/pareto_audit.json").write_text("not valid JSON", encoding="utf-8")
        assert report.validate_report(root, final=True)["passed"]
        checks += 1

        for mode in ("single_point", "relaxation"):
            task_root = fixture(base / mode, tasks=(mode,))
            evidence, _ = write_reviewable(task_root)
            assert report.validate_report(task_root, final=True)["passed"]
            assert not evidence["comparison"]["ranking_available"]
            unsupported = "relaxed_adsorption_energy" if mode == "single_point" else "candidate_ordering"
            assert evidence["use_cases"][unsupported]["status"] == "not_evaluated"
            checks += 1

        bad = fixture(base / "excluded", bad=True)
        evidence, _ = write_reviewable(bad)
        assert evidence["status"] == "ready_for_review"
        assert len(evidence["models"]) == 2 and evidence["comparison"]["included_model_keys"] == ["a"]
        assert evidence["models"][1]["review_status"] == "excluded"
        assert report.validate_report(bad, final=True)["passed"]
        assert ranking(bad, "--reliability-threshold", "0.5") == 0
        evidence, _ = write_reviewable(bad)
        assert evidence["comparison"]["n_relax_common"] == 2
        assert abs(evidence["models"][1]["reliability"]["rate"] - 2 / 3) < 1e-12
        checks += 2

        # Changing saved settings requires ranking regeneration, even if metrics did not change.
        config_path = bad / "summary/ranking_config.json"
        config = load_json(config_path)
        config["reliability_threshold"] = 0.4
        atomic_write_json(config_path, config)
        fails(bad, "ranking audit is stale")
        checks += 1

        tied = fixture(base / "tied")
        tied_rows = report.read_csv(tied / "summary/benchmark_predictions_long.csv")
        for row in tied_rows:
            row["model_adsorption_energy_eV"] = float(row["dft_adsorption_energy_eV"]) + 0.1
        csv_file(tied / "summary/benchmark_predictions_long.csv", tied_rows)
        assert ranking(tied) == 0
        evidence, _ = write_reviewable(tied)
        assert {m["ranking"]["rank"] for m in evidence["models"]} == {1}
        assert len(evidence["use_cases"]["single_point_energy"]["eligible_model_keys"]) == 2
        checks += 1

        for name, options, group in (("constant", {"constant": True}, "candidate_ordering"),
                                     ("disjoint", {"disjoint": True}, "single_point_energy")):
            edge = fixture(base / name, **options)
            evidence, _ = write_reviewable(edge)
            assert evidence["use_cases"][group]["status"] == "insufficient_evidence"
            assert report.validate_report(edge, final=True)["passed"] == (name != "disjoint")
            checks += 1

        failed = fixture(base / "failed", tasks=("single_point",))
        rows = report.read_csv(failed / "summary/benchmark_predictions_long.csv")
        for row in rows:
            if row["model_key"] == "b":
                row["status"] = "failed"
                row["model_adsorption_energy_eV"] = ""
        csv_file(failed / "summary/benchmark_predictions_long.csv", rows)
        csv_file(failed / "summary/model_run_status.csv", [{"model_key": "a", "final_status": "complete"}, {"model_key": "b", "final_status": "failed", "notes": "model load failed"}])
        evidence, _ = write_reviewable(failed)
        assert evidence["models"][1]["run_status"]["final_status"] == "failed"
        assert evidence["models"][1]["coverage"]["SP"] == 0
        assert evidence["comparison"]["n_sp_full"] == 3
        checks += 1

        invalid = fixture(base / "fixed")
        rows = report.read_csv(invalid / "summary/benchmark_predictions_long.csv")
        rows[-1]["fixed_atom_max_displacement_A"] = "0.1"
        csv_file(invalid / "summary/benchmark_predictions_long.csv", rows)
        assert invoke("build-evidence", invalid) == 1
        checks += 1

        missing = fixture(base / "missing_ranking")
        assert invoke("build-evidence", missing) == 0
        data = report.build_evidence(missing)
        assert data["comparison"]["ranking_status"] == "missing"
        # Missing audit is an explicit final gate, independent of prose review.
        (missing / "summary" / report.REPORT).write_text(report.draft(data).replace("TODO", "Fixture explanation."), encoding="utf-8")
        fails(missing, "fresh ranking audit", final=True, require_review=False)
        checks += 1

        # Rehashed but numerically falsified CSV still fails recomputation.
        from common import sha256_file
        tampered = fixture(base / "tampered")
        assert ranking(tampered) == 0
        evidence, _ = write_reviewable(tampered)
        output = tampered / "summary/model_ranking.csv"
        rows = report.read_csv(output)
        rows[0]["total_score"] = "0.123"
        csv_file(output, rows)
        audit_path = tampered / "summary/ranking_audit.json"
        audit = load_json(audit_path)
        audit["output_sha256"]["model_ranking.csv"] = sha256_file(output)
        atomic_write_json(audit_path, audit)
        fails(tampered, "ranking metric drift")
        checks += 1

        # Audit integration, with unrelated scientific gates mocked only here.
        import audit_completion
        with patch.object(audit_completion, "validate", return_value={"errors": [], "warnings": []}), \
             patch.object(audit_completion, "validate_report", return_value={"passed": False, "errors": ["sentinel report failure"]}):
            minimal = base / "audit_gate"
            minimal.mkdir()
            result = audit_completion.audit(minimal)
            assert "model selection report: sentinel report failure" in result["errors"]
            assert result["model_selection_report_passed"] is False
        import run_control
        with patch.object(run_control, "require_owner"), patch.object(run_control, "load_core", return_value=({}, {}, {})), \
             patch.object(report, "validate_report", return_value={"passed": False, "errors": ["stale report"]}):
            try:
                run_control.finalize_run(base, "owner")
                raise AssertionError("finalize must reject stale report before cached audit")
            except run_control.ControlError as exc:
                assert "stale report" in str(exc)
        checks += 2
    print(f"Model selection report self-test passed ({checks} scenario groups)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
