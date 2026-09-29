# English model selection report

Deliver `summary/model_selection_report.md` for every benchmark, including SP-only
and Relax-only runs. This is an agent-authored interpretation of measured results,
including the verified weighted final ranking. Read this reference after merge and
weighted ranking, before final audit. Write final model statuses and reproducibility
notes first so report evidence fingerprints the final inputs.

## Evidence and authoring sequence

Use an explicit generic Python interpreter with these commands:

```text
python scripts/model_selection_report.py build-evidence <benchmark_root>
python scripts/model_selection_report.py draft <benchmark_root>
```

The first command reads merged results and creates
`summary/model_selection_report_evidence.json`. It does not run predictions or
replace existing metrics. The second creates an English draft only and refuses
to overwrite an existing report. Complete its prose as the agent. A draft with
TODO placeholders cannot pass validation. To refresh an existing report, rebuild
evidence, replace the generated evidence block using the tool's `evidence_block`
function, and revise the interpretation; preserve useful existing prose.

Evidence includes every selected model (also disabled, failed and excluded
models), identity/version, final status, task coverage, per-model error statistics,
common-sample statistics and exact indices, failure records, five-class counts,
fixed-layer evidence coverage, inclusion decisions, scope/configuration and source
SHA-256 values. Missing optional inputs are recorded as absent; later creation
invalidates the old evidence. Runtime state and completion-audit files are not
fingerprinted because finalization changes them.

For quantitative claims in editable prose, use verified Markdown links to JSON
pointers, for example `[0.12](model_selection_report_evidence.json#/models/0/full_sp/mae_eV)`.
The label MUST match the tool's `display` representation (floats use eight
significant digits). Generate links with `cite(evidence, pointer)`; do not hand
round numbers or invent percentages. These pointers also support scalar or
structured scope fields, formulas, versions and exception records. Model names
may appear literally. All other numerical literals in editable prose must be
evidence-linked; avoid numbered prose lists. Keep the generated verified evidence
block unchanged. Source paths are relative to the report and link to actual run
artifacts; unrequested or unavailable artifacts must not be presented as results.

## Report structure and decisions

Keep the draft's section headings, use-case markers, assessment statuses and
model markers. Write English prose, including:

- **Executive Summary**: a use-case/candidates/evidence/conditions matrix and
  concise conclusions. More than one candidate can be appropriate.
- **Benchmark Scope**: dataset, adsorbate, model versions, requested tasks, DFT
  target and geometry semantics, common sample counts, coverage and exceptions.
- **Recommendations by Use Case**: evaluate the four cases below, citing the
  corresponding model evidence. The evidence's eligible-model list is a data
  availability list, NOT an automatic recommendation.
- **Model-by-Model Assessment**: strengths, limitations, suitable work and
  conditions for every selected model. A failed/skipped/excluded model needs an
  explicit reason and evidence; it cannot disappear from the report.
- **Robustness and Limitations**: common samples, error dispersion and bias,
  available uncertainty, coverage, reliability decisions and protocol exceptions.
- **Evidence Tables**: generated tables and fingerprinted source links.

| Use case | Primary evidence | Required interpretation |
|---|---|---|
| Single-point adsorption energies | Full-dataset SP MAE, bias, median/max error, error dispersion | Relative accuracy on the tested data; no universal acceptable MAE threshold |
| Candidate ordering and screening | Full-dataset SP Spearman | Correlation is not screening hit rate or absolute-energy accuracy |
| Structure preoptimization | Full-denominator intact-and-converged rate, five-class failures, fixed-layer checks | Reliability does not prove proximity to DFT minima; flag dissociation, detachment and missing evidence |
| Relaxed adsorption energies | Common reliable Relax MAE plus full-denominator reliability and coverage | Small common subsets qualify any recommendation |

Present the final ranking from verified weighted scores and configured weights, then
interpret task-specific tradeoffs. A higher weighted score is a relative preference
within the included cohort, not statistical significance or universal suitability.
Do not invent alternative weights, absolute cutoffs or uncertainty estimates.

The report is restricted to this adsorption dataset and protocol. Do not make
speed, memory, cost, molecular dynamics, reaction barrier or untested chemistry
claims. Do not treat Matbench selection rank as adsorption evidence. For
preoptimization, explicitly distinguish retained adsorption geometry from
demonstrated accuracy of the geometry relative to DFT.

## Partial tasks, review and unavailable comparisons

SP-only or Relax-only runs assess only supported use cases; others retain
`not_evaluated`. Null correlation, empty common sets or no usable frames mean
insufficient evidence, not a zero error or a poor numerical score. Explicitly
excluded/disabled models stay in the model assessments but not the common
comparison. Failed models retain their expected frame denominator. Reliability filtering applies
when Relax is requested; SP-only full-dataset metrics remain unavailable for incomplete models.

Below-threshold Relax models are automatically excluded. Preserve all model
assessments and reasons. There are no per-model inclusion overrides or pending
reliability decisions in this release.

Use only `summary/model_ranking.csv` and `summary/ranking_audit.json`, bound to
`summary/ranking_config.json` and current inputs. The evidence builder recomputes
scores and ranks and checks every output hash; stale or altered evidence is rejected.
The generated evidence table includes final ranks, scores, contributions and settings.
Do not read legacy Pareto/Bootstrap artifacts. For full SP+Relax runs, final completion
requires a current complete or explicitly unavailable ranking audit. SP-only and
Relax-only reports are valid without a composite ranking; state that limitation.
Do not infer confidence intervals or uncertainty-based superiority from scores.
All-numerical Relax metrics and intact-only auxiliary metrics remain distinct from
common Intact-and-converged metrics.

## Review and completion gate

Actually review each model and use-case claim for evidence support, task scope,
fair sample selection, numerical accuracy and limitations. Then record the
review (an agent identifier is acceptable):

```text
python scripts/model_selection_report.py record-review <benchmark_root> --reviewed-by <reviewer-id>
python scripts/model_selection_report.py validate <benchmark_root> --final
```

The review writes `summary/model_selection_report_review.json`, bound to report
and evidence hashes and listing every reviewed model/use case. Never record this
attestation without reading the report against the evidence. Automatic checks
cannot establish that arbitrary qualitative prose is scientifically justified;
that is the reviewer's responsibility. They do check source freshness, evidence
reproduction, report/model/use-case coverage, bound numerical claims and review
freshness. Any prose edit invalidates the review; any evidence input change
requires extraction, revision and review again.

`scripts/audit_completion.py` requires a valid final report. Finalization also
revalidates the report so an old passing audit cannot bypass stale evidence.

Regression command: `python scripts/model_selection_report_self_test.py`.

## Version-specific catalytic configuration

Evidence extraction now requires each model's confirmed catalysis_assessment,
execution_variant and leaderboard_identity. The verified table includes actual
checkpoint, generic task parameters, head and selection rationale. Fill the
Configuration choice field for every model using official source evidence, and
explain any difference from the original Matbench configuration. The decision
reason is an applicability argument, not proof that the chosen branch performs
better; support performance statements only with this benchmark's measurements.

Evidence schema 3 distinguishes full_sp and common_relax. all_valid_sp is
diagnostic only when coverage is partial. Include full dataset size, each model's
SP coverage and missing/failed indices from sp_completion. Old evidence must be
rebuilt and reviewed. pending_sp_completion cannot pass final validation.

Evidence schema 3 distinguishes full_sp and common_relax. all_valid_sp is
diagnostic only when coverage is partial. Include full dataset size, each model's
SP coverage and missing/failed indices from sp_completion. Old evidence must be
rebuilt and reviewed. pending_sp_completion cannot pass final validation.
