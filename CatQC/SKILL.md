---
name: catqc
description: >
  Build, resume, validate, and audit adsorption-energy benchmarks for either a
  user-selected top N from the Matbench Discovery leaderboard or an explicit
  user-provided list of MLP/foundation models, using single-point energy,
  structural relaxation, or both. Trigger for English or Chinese requests that
  combine adsorption evaluation with Matbench ranking or named models. For
  named models, search authoritative sources, show candidate links, and require
  user identity confirmation before setup. Do not trigger for general MLP
  installation or an already-produced results-only analysis unless the user
  explicitly invokes $catqc.
---

# CatQC

CatQC is a standalone adsorption-energy quality-control project and agent skill. This release is
`catqc-20260929`, derived from the frozen `20260920-autocata-catqc-english`
release. This is a frozen release. Do not modify it in place; create a new derived version
for future changes. Keep the original release unchanged.

Use this project-local skill from its packaged location. New run artifacts belong
under `<workspace_base>/CatQC/benchmark_runs`, never inside this skill directory.
Do not install it into the personal skill library unless the user asks.
Documentation is maintained in English; respond in the user's preferred language.

Treat the directory containing this file as `skill_root`. Resolve `references/`,
`assets/`, `scripts/`, and `agents/` relative to it. Create or resume a separate
`benchmark_root`; never write run artifacts inside `skill_root`.

Structure inputs may be a multi-frame `.xyz`/`.extxyz`, a single `.cif`,
`POSCAR`/`CONTCAR`, or a VASP-format `.vasp` file, or a directory of one supported format. ASE normalizes all
inputs to copied `Atoms` frames; the VASP reference branch writes standard
POSCAR files only in per-calculation input directories.

## Parse The Request

Read `references/request-model-selection.md` and
`references/catalysis-aware-selection.md` first. For every selected model, inspect
its official GitHub at the exact version for catalytic task selectors, heads and
checkpoints. Compare energy/reference compatibility and obtain combined model/
configuration confirmation before Step 0. Preserve original Matbench identity
separately from the tested configuration; never hardcode a provider task name. Extract these request fields:

- `model_selection_mode`: `matbench_top_n` or `user_specified`.
- For `matbench_top_n`, `model_count`: positive integer N.
- For `user_specified`, `requested_models`: nonempty ordered list preserving the
  user's exact model names; derive `model_count` from its length.
- `dataset_name`: dataset name or path, plus a stable dataset identity/version
  or content hash when available.
- `adsorbate`: required requested adsorbate identity; Step 1 verifies it against
  the structures but never renames the run.
- `requested_tasks`: `single_point`, `relaxation`, or both.
- `dft_reference_mode`: `provided` when the user supplies a valid aligned DFT
  adsorption-energy table; otherwise `generate_vasp`. The user may explicitly
  request recomputation even when a table exists.
- `workspace_base`: required user-selected path prefix. Construct the run base
  only as `<workspace_base>/CatQC/benchmark_runs`; never infer the prefix
  from `skill_root`, the current directory, or a dataset path.

Map Chinese and English equivalents of single-point/static energy to
`single_point`, and relaxation/geometry optimization to `relaxation`. Ask only
for a missing or ambiguous required field, including `workspace_base`. Do not
silently choose a dataset, model, model count, task, leaderboard, DFT target,
energy unit, or run method.

For `user_specified`, search each requested name before Step 0. Prefer official
repositories, model cards, package documentation, provider pages, and primary
papers. Show the user the proposed identity and at least one direct source link
for every model, then require explicit confirmation. Do not create a run,
download weights, install packages, or substitute a similarly named model until
all identities are confirmed. If no credible candidate is found, ask the user
for identifying information such as an official URL, repository, paper/DOI,
package/provider, organization, checkpoint path, or exact model-card name.

Activate for requests such as:

```text
Benchmark the top 20 Matbench Discovery models on the NO3 adsorption dataset using single-point energy and relaxation.
Benchmark the top 12 Matbench Discovery models on the supplied CO adsorption set; run relaxation only.
Benchmark MACE-MP-0 and CHGNet on the supplied NO3 adsorption dataset with single-point energy.
```

Do not implicitly activate for:

```text
Install MACE for general use.
Plot the completed adsorption benchmark results.
```

## Establish Scope And Authority

Before external changes, distinguish read-only discovery from mutations:

- Inspect local files, environments, scheduler state, and public documentation
  without asking again.
- Ask before creating or modifying environments, installing packages, enabling
  network access, downloading a checkpoint larger than the locally configured
  approval threshold, accepting a license, submitting a paid/long-running job,
  or changing scientific definitions.
- Never ask the user to paste a raw token into chat. Ask them to authenticate in
  the provider CLI/session or set a secret environment variable, then verify only
  the authenticated state. Never persist secret values.

## Run The Workflow

Complete the model-identity confirmation in
`references/request-model-selection.md` before Step 0, then read each reference
immediately before its step:

0. Initialize or resume the v4 run and acquire its lease: `references/step0-workdir.md`
1. Discover inputs and freeze scientific definitions: `references/step1-inputs.md`
2. Resolve repositories, licenses, environments, checkpoints, and access: `references/step2-leaderboard.md`
3. Configure and run models: `references/step3-model-run.md`
4. Merge predictions and calculate metrics: `references/step4-merge.md`
5. Write the English model selection report, audit and deliver the run: `references/step5-final-audit.md`

When `dft_reference_mode=generate_vasp`, read
`references/dft-vasp-reference.md` during Steps 1 and 3. Run the VASP DFT and
MLP branches concurrently when resources permit; Step 4 waits for the DFT gate.

During Step 2, read `references/step2-browser-dom-extraction.md` only for
`matbench_top_n`. During Step 3,
read `references/step3-script-calculator-integrity.md` and conditionally read
`references/runtime-compatibility.md` when running in the known sari06/Hermes
cluster or when a matching failure occurs. Use `references/evaluation-cases.md`
when testing or revising this skill. When Relax is requested, also apply the
source-agnostic protocol in `references/relaxed-structure-classification.md` to
both MLP and VASP slab+adsorbate relaxed structures. After DFT/MLP rows are
merged and validated, read `references/model-ranking.md` and use
`scripts/rank_models.py` for reliability filtering and normalized weighted ranking.
Ask together whether to accept reliability >= 0.95 and Relax MAE:SP MAE:Spearman
weights 5:3:2 or customize them. Reuse previously explicit choices and saved
`summary/ranking_config.json`; do not ask again on resume.
Before final audit, read `references/model-selection-report.md` and deliver an
English, evidence-linked report recommending models for the tested adsorption
use cases. Cover all selected models, including failures and exclusions; do not
claim speed/cost or untested-task suitability. SP-only and Relax-only reports
must mark unsupported use cases not evaluated.
Every Relax frame must also pass the coordinate-level fixed bottom-layer gate:
the MIC displacement of the automatically selected fixed atoms must be no more
than `fixed_atom_max_displacement_A` (frozen at `0.05 Å`), with indices and
displacement evidence retained in the frame output.

Before creating any model environment, require the per-model
`environment_preparation.json` record defined by
`assets/environment_preparation.schema.json`: official installation guides
must be saved and hashed, the host/CUDA/toolchain snapshot must be captured,
and the toolchain decision must follow `uv` (user-owned, then agent-owned),
`conda`/`mamba`, then an explicit user decision about installing `uv`.
Before generating or running any model-specific test script, require a frozen
official model-usage plan. Capture usage guides with
`environment_preparation_control.py capture-guide --guide-scope usage` (or
`both` when the same document is also the installation guide), freeze
the plan with `model_usage_control.py freeze-usage-plan`, and validate it before
script generation and runtime execution.

## Use Deterministic Tools

Use the bundled scripts instead of rewriting their logic:

- `scripts/validate_skill.py <skill_root>` validates package links and JSON assets.
- `scripts/catalysis_selection.py <selection-file.json>` checks version-bound
  GitHub evidence, candidate applicability, energy semantics and explicit selection.
- `scripts/run_control.py init-or-resume ...` creates or reconciles a v4 run,
  acquires the single-writer lease, and refuses v2/v3 migration.
- `scripts/run_control.py initialize-frames ...` freezes the Step 1 frame count.
- `scripts/run_control.py reconcile ...` rebuilds checkpoints from verified
  result rows and reattaches active jobs without duplicate submission.
- `scripts/run_control.py record-frame ...` commits frame completion only after
  its validated per-frame manifest, result/error artifact, and cleanup evidence
  exist. Use `frame-paths` and `validate-frame` for model-owned runners.
- `scripts/dft_reference_control.py validate-provided ...` validates and freezes
  a user-supplied DFT table.
- `scripts/dft_reference_control.py prepare|preflight|submit|reconcile|collect|approve-partial ...`
  requires a secret-free remote runtime probe before formal VASP submission;
  the probe covers shell stack limits, MPI/VASP versions, scheduler resources,
  and a representative execution smoke test.
- `scripts/dft_reference_control.py monitor ...` diagnoses active DFT output;
  DFT has no Skill-imposed walltime, per-calculation timeout, or idle timeout.
  Only frozen output rules or explicit external scheduler termination may stop
  a DFT calculation.
- `scripts/probe_environment.py --output ...` captures the read-only host/CUDA
  snapshot, and `scripts/environment_preparation_control.py capture-guide|
  select-toolchain|validate ...` gates model environment creation.
- `scripts/environment_install_control.py plan|authorize|create|auto-create ...` creates a
  dry-run or explicitly authorized environment from the frozen installation
  plan and records observed runtime evidence.
- `scripts/model_usage_control.py freeze-usage-plan|validate-usage-plan|
  validate-official-smoke-test|prepare-model-script ...` freezes and verifies
  official model API, checkpoint, device, units, official smoke-test evidence,
  calculator isolation, and model-owned script hashes before scripts may run.
  controls the resumable DPDispatcher/VASP reference branch.
- `scripts/validate_run.py <benchmark_root>` validates artifacts and cross-file
  invariants before resume, merge, and completion.
- `scripts/merge_benchmark_results.py <benchmark_root>` creates versioned summary
  tables from validated per-model CSVs and `summary/dft_reference.csv`, and
  exports one final POSCAR per valid Relax frame plus its index CSV.
- `scripts/relaxed_structure_classification.py` applies the shared mutually
  exclusive five-class Relax classification to normalized MLP/VASP records.
- `scripts/classify_relaxed_frame.py` adapts one MLP or VASP frame to that
  classifier without mixing clean-slab or isolated-adsorbate DFT objects into
  adsorbate geometry classification.
- `scripts/rank_models.py <benchmark_root>` reuses saved ranking settings.
  First use requires the user's default/custom choice: `--accept-defaults`, or
  `--reliability-threshold <0..1>` and/or `--weights <relax> <sp> <rho>`.
  Record the user's choice via `--selection-source`. Below-threshold models are
  excluded automatically; the three common-sample metrics are min-max normalized
  and weighted. Read `references/model-ranking.md` for output and edge cases.
- `scripts/model_selection_report.py build-evidence|draft|record-review|validate <benchmark_root>`
  extracts source-hashed report evidence, scaffolds an English agent-authored
  report, and validates coverage, numerical links and hash-bound semantic review.
  A draft or stale/missing required ranking audit cannot satisfy final completion.
- `scripts/audit_completion.py <benchmark_root>` reproduces metrics and checks the
  final deliverable set.

Run scripts with an explicit generic Python 3 interpreter. They use only the
standard library and must not require installation into a model environment.

## Freeze The Scientific Definition

Do not launch a model until `benchmark_config.json` records and validates:

- source structure and DFT paths, frame count, order/alignment, and energy unit;
- adsorbate indices/selection, boundary handling, and slab composition rule;
- a per-model adsorbate reference policy including geometry source, charge,
  spin multiplicity, PBC, cell/vacuum, stress setting, relaxation state, and
  whether immutable model weights may be reused;
- task-specific DFT target semantics;
- relaxation optimizer, maximum steps, cell policy, constraints, layer-detection
  tolerance, force criterion, dissociation rule, and exception policy;
- a scientific-definition hash stored in the run manifest.

For `matbench_top_n`, treat Matbench rank as an external model-selection proxy,
not an adsorption performance ranking. Keep `snapshot_rank` separate from
adsorption metrics and state this limitation in final notes. For
`user_specified`, set `snapshot_rank` to null and use the one-based
`selection_index` only to preserve the user's order; never present that order as
a ranking.

## Preserve Execution Integrity

- Keep original structures, DFT tables, and user checkpoints read-only.
- Freeze the selected model identities and never silently replace a model.
- Bind execution_variant and catalysis_assessment to the model identity, official
  usage plan, smoke test and observed calculator initialization for each energy
  component. Missing or differing task parameters, heads or checkpoints block
  execution/completion. Old v4 runs without this evidence require their original
  skill or a newly confirmed run; do not migrate them silently.
- Require a confirmed authoritative link for every `user_specified` model and
  preserve the user's original name, confirmed identity, source URL, and
  confirmation record.
- Use model-owned executable SP/Relax scripts; share only stateless I/O and
  validation helpers.
- Execute one structure at a time. Every structure uses a fresh calculator and
  a canonical directory under `models/<model_key>/frames/<task>/<index>/`.
  `frame_manifest.json` is authoritative for that structure; aggregate CSV is
  a derived convenience artifact and aggregate Relax XYZ is not required.
- SP must write one `outputs/result.json` per structure. Relax must additionally
  write one internal `outputs/relaxed_structure.extxyz` per structure. These are
  audit inputs, not the final delivery format: final Relax delivery is one
  standard POSCAR per valid frame under `summary/structures/Relax/`. Missing
  frame artifacts, cleanup proof, or cross-frame output reuse invalidates the
  frame.
- Reject script paths outside `models/<model_key>/scripts/`, symlinks, duplicate
  paths or duplicate script hashes, shared/delegated runners, and incomplete
  model-ownership attestations. Require runtime provenance proving that each
  task used the model-configured Python executable, owned script hash, and
  recorded package versions.
- Require a fresh calculator object for each structure/component. Permit reuse
  of immutable loaded weights only when the provider supports it and A→B/B→A
  order-invariance plus object-identity checks pass. Never reuse mutable
  neighbor lists, results, optimizers, work directories, or calculator state.
- Record every scientific or runtime exception with reason, scope, approver,
  timestamp, and before/after definition. Do not tune per model silently.
- Keep checkpoints below `models/<model_key>/checkpoints/` with file-level size
  and SHA-256 records. Do not use an external symlink as the only copy.
- Normalize statuses to the controlled vocabulary in the assets. Preserve every
  failed attempt and never leave the only result in logs.
- Apply the shared result-validity assessment before metrics. Require finite
  decomposed energies, verify the adsorption-energy identity, reject conflicting
  status/convergence fields, and verify successful Relax rows against the
  configured force threshold. Preserve the source status and validation reason.
- Enforce the Relax baseline: fixed cell, FIRE, `fmax=0.05 eV/A`, and 1000
  maximum steps. Use BFGS only when FIRE is genuinely unsupported and record the
  fallback reason plus scientific-exception ID. Automatically rerun a
  1000-step unconverged frame with 5000 steps; exclude it if still unconverged.
  Fix the bottom 1 layer for slabs with at most 5 layers and the bottom 2 layers
  for slabs with at least 6 layers.
- Mark incomplete coverage acceptable only after bounded recovery and explicit
  user approval.
- Classify DFT infrastructure failures separately from scientific
  nonconvergence. A `SIGSEGV` associated with a propagated soft stack limit is
  `soft_stack_sigsegv`; repair may only change the execution wrapper and must
  preserve the frozen VASP profile.

## Monitor Without Duplicating Work

After submission, record the job ID/PID and authoritative v4 job-status artifact.
Heartbeat `control/lease.json` every 60 seconds. Step 3 persists the per-frame
manifest, result or error, and cleanup evidence first, validates the complete
frame directory, atomically updates the task checkpoint and central state, then
appends an event. On resume, Step 0 treats verified per-frame artifacts as the
completion source of truth and reconciles every nonterminal job before
enabling submission. A missing process or disappearance from `squeue` is not
proof of success. A 15-minute-expired lease may be taken over only with external
job reconciliation; active jobs are monitored, never resubmitted.

Use a persistent monitor or task wakeup when available. Ask the user only for a
decision, credentials setup, new resources, or a scientific-definition change;
do not ask them to report routine job completion.

## Completion Gate

Before declaring success:

1. Run `scripts/validate_run.py`.
2. Run `scripts/merge_benchmark_results.py` when summaries are absent or stale.
3. Write model statuses and reproducibility notes, then generate and review the
   English report using `references/model-selection-report.md`. Validate it with
   `scripts/model_selection_report.py validate <benchmark_root> --final`, then
   run `scripts/audit_completion.py`.
4. Confirm every selected model is complete, explicitly skipped, gated, failed,
   or blocked with a visible reason.
5. Confirm DFT order, frame coverage, status normalization, dissociation records,
   scientific-definition hash, checkpoint manifests, script hashes, environment
   provenance, and metric reproduction.
6. After the audit passes and every requested checkpoint is complete, run
   `scripts/run_control.py finalize-run <benchmark_root> --owner-id <owner>`.
   Otherwise leave the run incomplete and record the next action.

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
