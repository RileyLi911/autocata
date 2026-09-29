# Reliability filtering and weighted ranking

## User settings and resume behavior

Before final ranking, ask together: "Use the default reliability threshold of
0.95 and Relax MAE:SP MAE:Spearman weights of 5:3:2, or customize them?"
Reuse choices already explicitly supplied by the user. If
`summary/ranking_config.json` exists, resume with those saved choices.
Elapsed time is not acceptance. Without initial settings, the script returns 2
and asks the agent to obtain the user's choice.

After the user accepts the defaults:
```text
python scripts/rank_models.py <benchmark_root> --accept-defaults --selection-source "user accepted defaults"
```
After a custom choice, for example:
```text
python scripts/rank_models.py <benchmark_root> --reliability-threshold 0.9 --weights 6 3 1 --selection-source "user supplied custom settings"
```
To resume, pass only benchmark_root. Supplying just the threshold or weights
preserves the other saved setting; on first use, the other setting uses the default
accepted by the user. `--accept-defaults` explicitly resets both defaults, with
explicit values in the same command overriding the corresponding default.

See `assets/ranking_config.schema.json`. The script also enforces finite numbers,
a positive total weight, and consistency between raw and normalized weights.
The configuration records schema_version, reliability_threshold, raw_weights,
normalized_weights, and selection_source. The threshold must lie in [0,1].
Weights are ordered Relax MAE, SP MAE, Spearman; all must be nonnegative and
at least one must be positive. After settings change, rerun ranking and refresh
report evidence and its review record.

## Reliability filtering and sample policies

R_relax = n(Intact and converged) / n(expected Relax structures).
Missing frames remain in the denominator. Models with R_relax < threshold are
automatically excluded; equality passes. Disabled models do not enter ranking.
Retain category counts, reliability, filtering status, and reasons for every model.
The five-category priority and fixed-layer checks are unchanged. Reject duplicate
frames, invalid frames labeled reliable, and inconsistent DFT references.

```text
N = size of the full frozen original structure_indices list
K = size of the intersection of valid Intact and converged Relax indices across included models
SP MAE_N = mean(abs(E_SP_model - E_DFT))
Relax MAE_K = mean(abs(E_Relax_model - E_DFT))
rho_N = Spearman(E_SP_model, E_DFT), on every original SP sample
```

## Scores and ranks

Apply min-max normalization separately to each metric across included models:
```text
s_MAE = (max(MAE) - MAE) / (max(MAE) - min(MAE))
s_rho = (rho - min(rho)) / (max(rho) - min(rho))
S = w_relax * s_relax + w_sp * s_sp + w_rho * s_rho
```
Default normalized weights are 0.5, 0.3, and 0.2. If a metric is constant across
models, assign that metric a score of 1 to every model.
Sort S descending. Scores within 1e-12 of the highest score in a tie group share
competition rank (1,1,3). Within a tie, model name and then model_key determine
display order only. Scores depend on the cohort; they are neither absolute accuracy
nor statistical significance.

Do not produce numerical ranks if no models remain, a common sample set is empty,
or any required metric is undefined. This includes Spearman for a constant series
or fewer than two SP samples. Do not impute scores or redistribute weights.
Even a zero-weight metric must be computable. SP-only and Relax-only runs may
produce metric reports, but not this three-metric ranking.
A successfully recorded unavailable comparison returns 0 with status=unavailable;
that exit code does not mean a ranking exists.

## Outputs and acceptance

- `summary/model_reliability_review.csv`: every model and filtering reasons.
- `summary/model_ranking.csv`: original metrics, full SP and common Relax sample counts, normalized
  scores, weighted contributions, total score, and rank for included models.
- `summary/ranking_config.json`: settings and user-choice provenance.
- `summary/ranking_audit.json`: status, reason, cohort, sample indices, settings,
  and input/output SHA-256 hashes.
- `summary/methodology.txt`: reproducible method description.

When ranking is unavailable, replace the previous ranking CSV with a header-only
file to prevent stale ranks from being reused. The report recomputes all ranking
values and verifies source and output hashes. Changes to data or settings invalidate
old evidence. Full SP+Relax completion requires a fresh complete or unavailable audit.
Legacy Pareto/Bootstrap files are neither generated nor read. They may remain as
history but do not enter CatQC evidence. The retired script prints a migration
notice only and no longer runs the old analysis.

## Full-dataset coverage gate (audit schema 2)

`benchmark_config.json.structure_indices` freezes the complete input ordering
0 through frame_count - 1 before model execution. Its hash is part of the audit.
SP references must also match summary/dft_reference.csv, whose hash is retained.
Every included model must have a valid SP prediction and finite DFT reference
at every index. Otherwise record pending_sp_completion, with per-model coverage,
missing/failed indices and reasons in sp_coverage. Replace old ranks with a
header-only CSV; no scores, imputation, reweighting or model exclusion as a
workaround. After repair, rerun ranking and regenerate the report.

The audit distinguishes full_sp from common_relax, records full_dataset_indices,
full_dataset_size, sp_structure_indices and relax_structure_indices. Relax
failure does not remove the corresponding valid SP frame. Pending SP completion
blocks final acceptance; exit code zero only means the state was recorded.

## Full-dataset coverage gate (audit schema 2)

`benchmark_config.json.structure_indices` freezes the complete input ordering
0 through frame_count - 1 before model execution. Its hash is part of the audit.
SP references must also match summary/dft_reference.csv, whose hash is retained.
Every included model must have a valid SP prediction and finite DFT reference
at every index. Otherwise record pending_sp_completion, with per-model coverage,
missing/failed indices and reasons in sp_coverage. Replace old ranks with a
header-only CSV; no scores, imputation, reweighting or model exclusion as a
workaround. After repair, rerun ranking and regenerate the report.

The audit distinguishes full_sp from common_relax, records full_dataset_indices,
full_dataset_size, sp_structure_indices and relax_structure_indices. Relax
failure does not remove the corresponding valid SP frame. Pending SP completion
blocks final acceptance; exit code zero only means the state was recorded.
