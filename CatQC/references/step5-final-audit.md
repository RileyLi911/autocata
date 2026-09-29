# Step 5: Final Audit And Delivery

Finish by making the benchmark reproducible and resumable.

After writing the final model status table and reproducibility notes below,
apply `references/model-selection-report.md` to extract evidence, author the
English report, review its scientific claims and validate it before audit.

## Audit Required Artifacts

Confirm existence of:

- `run_manifest.json`
- `models_manifest.json`
- `run_state.json`
- `events.jsonl`
- `control/lease.json` and `control/latest_reconciliation.json`
- `benchmark_config.json`
- `input_inspection_report.json`
- every requested `models/<model_key>/state/<task>_checkpoint.json`
- `models/<model_key>/checkpoints/checkpoint_manifest.json` and verified
  checkpoint files for every model that ran
- For `matbench_top_n`, timestamped evidence under `selection_evidence/` with rendered HTML,
  raw DOM rows, screenshots covering top N, and N-of-N sanity evidence
- For `user_specified`, complete `model_identity_confirmations` with direct
  authoritative URLs and explicit user confirmation for every model
- `selection_evidence/` with the frozen selection payload and source evidence
- `models/<model_key>/` directories with scripts, configs, logs, results,
  errors, validation reports, and per-frame internal artifacts when applicable
- For every executed model: saved official usage guide(s),
  `resources/model_usage_plan.json` and its `.sha256`,
  `validation/official_usage_smoke_test.json`, and
  `validation/script_preparation.json`.
- `summary/model_selection_report.md`
- `summary/model_selection_report_evidence.json`
- `summary/model_selection_report_review.json`
- `summary/benchmark_predictions_long.csv`
- `summary/benchmark_metrics_by_model.csv`
- `summary/benchmark_dissociation_records.csv` when Relax was requested
- `summary/benchmark_metrics_relax_intact_only.csv` when Relax was requested
- `summary/benchmark_missing_or_failed_frames.csv`
- `summary/benchmark_merge_report.json`
- `summary/relaxed_structure_index.csv` when Relax was requested
- `summary/structures/Relax/<model_key>/<structure_index>/POSCAR` for every
  valid converged Relax frame
- For supplied DFT: `dft/provided_reference_manifest.json` and its source/copy
  hashes.
- For generated DFT: `dft/vasp_profile.json`, `dft/execution_profile.json`,
  `dft/dft_manifest.json`, `dft/runtime_probe.json`,
  `dft/execution_preflight.json`, `dft/state/dft_state.json`, every calculation
  checkpoint/job status and diagnostics, and POTCAR TITEL/SHA provenance
  without POTCAR content.

Confirm original inputs were not modified.

## Final Status Table

Create:

```text
summary/model_run_status.csv
```

Columns:

```text
model_key,model_name,selection_index,snapshot_rank,enabled,environment_status,checkpoint_status,script_isolation_status,calculator_isolation_status,sp_status,relax_status,n_sp_valid,n_relax_valid,n_relax_intact,n_relax_dissociated,n_relax_integrity_unknown,final_status,notes
```

Use it to distinguish complete, skipped, gated, failed, and blocked models.

## Reproducibility Notes

Create:

```text
summary/reproducibility_notes.md
```

Include:

- Model-selection mode and frozen selection rule.
- For `matbench_top_n`, leaderboard timestamp, view settings, browser
  engine/version, effective rendered URL, extraction method, and DOM/screenshot
  match count.
- For `user_specified`, the user's original names, confirmed model identities,
  versions/variants, authoritative URLs, and confirmation timestamps.
- Requested model count, dataset name, and task selection.
- Input file paths and DFT target definition.
- DFT reference mode, energy formula, VASP profile hash and user confirmation,
  DPDispatcher execution profile, POTCAR backend/labels, DFT coverage, failures,
  and any explicit partial-coverage approval.
- Adsorbate definition and slab composition definition.
- Relaxation definition and constraints.
- Dataset-specific adsorbate dissociation definition and measured thresholds.
- Per-model script paths/hashes and calculator-isolation validation.
- Fixed checkpoint paths, revisions, file sizes, SHA-256 values, and manifest
  verification status.
- Dissociated model/frame records and the relationship between all-valid and
  intact-only Relax metrics.
- Environment-sharing decisions.
- Resource repairs or replacements.
- Gated/private model decisions.
- Known failures and user-approved skips.
- Exact output files for downstream analysis.

Do not include raw secrets, tokens, or credentials.

## Completion

Before reporting completion:

- Run `scripts/validate_run.py <benchmark_root>`.
- Run `scripts/model_selection_report.py validate <benchmark_root> --final`;
  missing/stale evidence, incomplete prose, omitted models or missing/stale ranking
  evidence prevent completion. Semantic review must match current report hashes.
- Run `scripts/audit_completion.py <benchmark_root>` and retain its JSON report.
- Only after the audit passes and every requested checkpoint is complete, run
  `scripts/run_control.py finalize-run <benchmark_root> --owner-id <owner>`.

- Confirm long and metrics CSVs are readable.
- Confirm MAE/RMSE are reproducible from the long table.
- Confirm failed frames are visible.
- Confirm every completed model has per-frame CSV outputs.
- Confirm every used checkpoint resolves under
  `benchmark_root/models/<model_key>/checkpoints/` and matches its manifest.
- For user-provided checkpoints, confirm the original source remained untouched,
  the runtime used the copied model-local path, and every required copied file
  is listed with size and SHA-256.
- Confirm every model used its own test scripts and passed calculator-isolation
  validation.
- Confirm each requested task's model-owned path/hash is unique, is not a
  symlink, does not delegate to a shared executable runner, and remains beneath
  the owning `models/<model_key>/scripts/` directory.
- Confirm `runtime_provenance.json` proves the observed Python executable,
  package versions, and executed task script hashes match `model_config.json`.
- Confirm the official usage plan schema, guide hashes, checkpoint hash,
  calculator class, GPU device, dtype, energy/force units, smoke-test evidence,
  script-preparation record, and per-task runtime provenance all match the
  frozen plan. Any changed guide, plan, checkpoint, smoke evidence, or script
  must invalidate the old run.
- Reject bare isolation pass flags; require the full object-identity,
  calculator-factory-call, separate-component, and order-invariance evidence.
- Confirm every Relax structure has an integrity status and all dissociated
  structures are marked in final outputs. Confirm every valid converged frame
  has exactly one readable POSCAR with a matching source manifest and SHA-256.
- Confirm dissociated frames were not silently removed from all-valid metrics.
- Confirm no model was silently dropped from frozen worklist.
- Confirm progress memory marks complete only after required artifacts exist.
- Set `run_manifest.json` status to `complete` only after every completion check
  passes.

If any required artifact is missing, leave benchmark status incomplete and record
the next action.

## Catalytic selection audit

Verify saved official GitHub content, checked version and selection decision,
usage-plan binding, script registration and observed component configurations.
Missing legacy evidence or mismatched checkpoint/task/head blocks completion.
The English report must show original leaderboard identity separately from the
actual configuration and distinguish intended domain from measured performance.

## Weighted ranking audit

For full SP+Relax requests, require a current ranking audit with complete or
unavailable status and its recorded reason. Verify saved threshold and weights,
automatic exclusions, common samples, output hashes, scores and ranks through
the report validator. A missing/stale ranking audit blocks final completion.
Partial-task reports explain why the three-metric ranking is not applicable.

A ranking status of pending_sp_completion blocks final acceptance. Complete SP
predictions and references for every frozen input index of every reliability-
passing model, then regenerate ranking, evidence and report review.
