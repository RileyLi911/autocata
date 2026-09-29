# CatQC

CatQC is a standalone adsorption-energy quality-control project and agent skill.
It organizes model selection, isolated model environments, official usage evidence,
frame-by-frame single-point (SP) and relaxation calculations, DFT references,
structure reliability checks, weighted comparison, and an evidence-linked report.

Display name: **CatQC**. Skill identifier: `catqc`. Invocation: `$catqc`.
Release: `catqc-20260929`, derived from `20260920-autocata-catqc-english`.
The frozen source release remains unchanged. All maintained content in this skill
is English; this does not restrict the agent's conversation language.

## Entry point and prerequisites

Use [SKILL.md](SKILL.md) as the agent's entry point. For example:

```text
Use $catqc to benchmark these explicitly selected models on my adsorption dataset
with both single-point and relaxation calculations. Use my aligned DFT reference
table. Ask for the workspace base and any missing dataset or model identity details.
```

Provide the dataset, adsorbate, SP/Relax task choice, model list or Matbench top-N
selection, and DFT reference mode. Confirm exact model versions and applicable
catalytic checkpoint/task/head choices using official sources. Input structures
support XYZ, extXYZ, CIF, POSCAR/CONTCAR, and VASP-format .vasp files.

Controllers and validators use an explicit generic Python 3 interpreter and the
standard library. Structure format conversion may require ASE. Model inference
uses each model's own verified environment, packages, checkpoint, and calculator.
Do not install controller dependencies into a model environment by convenience.
VASP generation additionally requires a usable VASP installation, an approved
execution profile, a licensed POTCAR source, and the configured submission tools.

## Workspace and compatibility

Ask for workspace_base before a new run; do not infer it from the skill or dataset.
New runs are created exclusively at:

```text
<workspace_base>/CatQC/benchmark_runs/<run-name>/
```

The skill directory is code and documentation, not a results directory. A run
contains models/, dft/, summary/, control/, manifests, checkpoints, and events.
Historical runs and source releases are not migrated. Resume them with their
original skill version. CatQC rejects old skill identities even if a legacy run
is copied into the new path.

## Resumable workflow

1. **Initialize or resume:** freeze request identity, model selection and
   dft_reference_mode; match incomplete runs and acquire the run lease.
2. **Validate inputs and references:** check structure order, adsorbate identity,
   scientific settings, DFT alignment and model provenance.
3. **Prepare isolated execution:** retain official guides, freeze usage plans,
   verify environments, checkpoints, smoke tests and calculator isolation, and
   register each model's own scripts.
4. **Run individual frames:** execute models sequentially and, by default, frames
   sequentially within each model. Record results or errors and cleanup evidence.
5. **Merge and compare:** validate frame artifacts, classify Relax outcomes,
   export one final POSCAR per valid Relax frame, merge metrics and compute the
   configured reliability-filtered ranking.
6. **Report and audit:** author and review the English model-selection report;
   finalization is allowed only after all required validation gates pass.

See [Step 0](references/step0-workdir.md), [model execution](references/step3-model-run.md),
[merge requirements](references/step4-merge.md), and [final audit](references/step5-final-audit.md).

## Model usage and execution isolation

Each model must retain official usage documentation, import and checkpoint
identity/hash, calculator construction, device, dtype, energy/force units,
SP/Relax protocol, smoke tests, and explained benchmark adaptations. Unresolved
conflicts block registration. Use guide scope usage or both where applicable.
Official catalytic applicability is not evidence of measured superiority.

[Configuration selection](references/catalysis-aware-selection.md) keeps original
leaderboard identity separate from the configuration actually evaluated.
Environment creation follows the documented toolchain selection and approval
policy; the agent must not guess APIs or silently switch checkpoints or devices.

Each frame has its own input, result/error record, artifact manifest and cleanup
evidence. Calculators, mutable structures, neighbor lists and result caches are
not shared between frames. Model environments, executable scripts, calculator
factories and mutable state remain isolated. Common scientific protocol does not
permit a shared model execution runner. Registration is performed through
scripts/run_control.py register-scripts after the required evidence passes.

## Relaxed-structure classification

MLP and VASP slab+adsorbate frames use the same mutually exclusive priority:

```text
Invalid -> Unconverged -> Dissociated -> Detached -> Intact and converged
```

- **Invalid:** missing/invalid evidence, changed atom count/order, severe overlap,
  catastrophic surface displacement, or failed fixed-layer checks.
- **Unconverged:** valid data/geometry without established optimization convergence.
- **Dissociated:** a monitored bond exceeds its configured threshold.
- **Detached:** an intact adsorbate exceeds both the configured final contact
  distance and contact-distance increase thresholds.
- **Intact and converged:** passes the preceding checks.

For severe overlap, the default condition is
`min(d_ij_MIC / (r_i_cov + r_j_cov)) < 0.55` over pairs with supplied positive radii.
The default catastrophic surface displacement threshold is 5.0 angstrom after
subtracting mean surface translation. Slabs with at most five layers fix the
bottom one layer; slabs with at least six layers fix the bottom two layers.
Every fixed atom must move no more than 0.05 angstrom under MIC.

Dataset configuration supplies atom indices, monitored bonds, radii, PBC and
thresholds in relaxation.relaxed_structure_classification. Example NO3 thresholds
may use N-O 1.8 angstrom, final contact 3.5 angstrom and contact increase
0.6 angstrom; these are examples, not universal adsorbate defaults.
Clean-slab and isolated-adsorbate DFT objects contribute to energy decomposition,
not adsorbate dissociation/detachment classification.

See [the classification protocol](references/relaxed-structure-classification.md).

## Reliability filtering and final ranking

Before ranking, ask once whether to accept or customize both defaults:

- Reliability threshold: **0.95**, configurable within [0,1].
- Relax MAE:SP MAE:Spearman weights: **5:3:2**, normalized to **0.5:0.3:0.2**.

Reuse explicit prior choices and saved summary/ranking_config.json on resume.
Reliability is the number of Intact and converged frames divided by the full
expected Relax frame count. Missing frames remain in the denominator. Models
below the threshold are excluded; models exactly at the threshold pass.

SP MAE and Spearman use every index in the frozen original dataset. Relax MAE uses the
common valid Intact and converged Relax intersection. Within included models:

```text
s_MAE = (max(MAE) - MAE) / (max(MAE) - min(MAE))
s_rho = (rho - min(rho)) / (max(rho) - min(rho))
S = 0.5 * s_Relax_MAE + 0.3 * s_SP_MAE + 0.2 * s_rho
```

Constant metrics receive score 1 for all models. Higher S ranks first; scores
within 1e-12 of the highest score in a tie group share competition rank. Model
names order tied display rows only. Scores are cohort-dependent preferences,
not absolute accuracy or statistical significance.

No numerical ranking is produced for an empty eligible cohort, empty common
samples or undefined metrics, including constant-series Spearman. No imputation
or automatic reweighting occurs. SP-only and Relax-only runs can produce reports
but cannot produce this three-metric ranking. Pareto and its Bootstrap analysis
have been replaced. See [ranking details](references/model-ranking.md).

```text
# After the user accepts defaults:
python scripts/rank_models.py <benchmark_root> --accept-defaults --selection-source "user accepted defaults"
# Custom settings after the user's choice:
python scripts/rank_models.py <benchmark_root> --reliability-threshold 0.9 --weights 6 3 1
# Resume without repeating the settings question:
python scripts/rank_models.py <benchmark_root>
```

## DFT reference modes

- **provided:** validate an aligned DFT adsorption-energy table without running VASP.
- **generate_vasp:** prepare consistent slab+adsorbate, clean-slab and isolated-
  adsorbate VASP calculations under a user-confirmed, frozen profile.

Select the POTCAR backend explicitly as vaspkit or direct, without silent fallback.
Do not commit or distribute POTCAR contents. Submission configuration uses SSH
agents/aliases or external environment variables, not stored passwords or tokens.
DFT execution preflight validates MPI/VASP, scheduler resources, shell stack limits
and representative smoke tasks. Profiles requiring unlimited stack must verify it
in both submission and job shells. Infrastructure errors are distinguished from
scientific nonconvergence. CatQC does not impose its own DFT walltime or idle
cancellation; MLP per-frame timeouts do not apply to DFT.
See [DFT protocol](references/dft-vasp-reference.md).

## Outputs and report

Each valid Relax frame is delivered as one POSCAR, linked to its source through
summary/relaxed_structure_index.csv. POSCARs must not be concatenated.
Merged predictions, metrics, missing/failed frame records and reproducibility
notes remain auditable alongside:

- summary/model_reliability_review.csv
- summary/model_ranking.csv
- summary/ranking_config.json
- summary/ranking_audit.json
- summary/methodology.txt
- summary/model_selection_report.md
- summary/model_selection_report_evidence.json
- summary/model_selection_report_review.json

The report covers SP energy accuracy, candidate ordering, structure preoptimization
and relaxed adsorption energies. It retains every selected model, including failures
and exclusions, with evidence and limitations. It does not infer unmeasured speed,
memory, cost, dynamics or other chemistry performance. Tools check source hashes,
numerical claims and recomputed scores/ranks; the agent must review the prose.
Missing/stale required evidence or an unreviewed draft blocks completion.
See [report instructions](references/model-selection-report.md).

## Validation

Run from this skill directory using the intended interpreter:

```text
python scripts/validate_skill.py .
python scripts/self_test.py
python scripts/rank_models_self_test.py
python scripts/model_selection_report_self_test.py
python scripts/catqc_identity_self_test.py
```

For an actual benchmark run:

```text
python scripts/validate_run.py <benchmark_root>
python scripts/model_selection_report.py validate <benchmark_root> --final
python scripts/audit_completion.py <benchmark_root>
```

Additional regression suites cover structure classification, frame artifacts,
model usage, catalytic selection, environment preparation and DFT orchestration.
The structure-input and DFT self-tests require --fixture-root. These synthetic
checks do not establish real-model accuracy or cluster performance.

[Validation record](references/validation_record.json) and
[release file hashes](references/release_freeze_manifest.json) document this release.
[source provenance](references/derivation_source_manifest.json) retains the exact
source release identifiers and hashes.

## Full SP coverage and DFT defaults

Freeze `structure_indices` in benchmark_config.json as the complete ordered list
from zero through frame_count minus one during input preparation. After reliability
filtering, use that full list for SP MAE and Spearman and only the shared valid
Intact and converged intersection for Relax MAE. Any included model with missing
or failed SP predictions or DFT references makes the entire ranking
`pending_sp_completion`: no scores or ranks until completion. Do not shrink the
dataset or exclude a model to work around missing SP. Diagnostic partial metrics
are not full-dataset metrics. This status blocks final acceptance.

For generate_vasp, use the unconfirmed `assets/vasp_defaults.json` proposal via
`scripts/dft_profile.py`. Show all three components, resolve actual POTCAR labels,
version/backend and initial magnetic moments, and obtain the user's choice.
The baseline is PBE, ENCUT 520 eV, EDIFF 1E-5 and Gamma-centered 2x2x1 for both
slab components; the isolated adsorbate uses Gamma-only 1x1x1 and 10 angstrom
vacuum per side. See `references/dft-defaults.md` for all settings, custom
overrides, preview and confirmation. Reuse unchanged confirmed settings on resume.
The provided-reference branch does not start VASP or replace reference parameters.
