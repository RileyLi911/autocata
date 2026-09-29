# Step 3: Run One Model At A Time

If `dft_reference_mode=generate_vasp`, start or resume the independent DFT
branch using `references/dft-vasp-reference.md`. DFT and MLP jobs may run at the
same time. Serialize controller writes with the lease, but do not serialize the
remote computations. Preserve separate DFT and per-model checkpoints.

Process models sequentially from the frozen worklist. Do not start the next
model until the current model is complete and validated, skipped with
user-approved reason, or blocked with a recorded pending question.

Read `references/step3-script-calculator-integrity.md` completely before writing
or validating any model test script.

Run only tasks listed in `benchmark_request.requested_tasks`. Mark an omitted
task `not_requested`; do not run it and do not ask whether to add it. If both
tasks are requested, validate each independently. If only relaxation is
requested, do not silently add a single-point calculation.

## Per-Model Workspace

Use `models/<model_key>/`:

```text
models/<model_key>/
|-- model_config.json
|-- environment_preparation.json
|-- environment_preparation.host_snapshot.json
|-- env_probe.json
|-- checkpoints/
|   |-- checkpoint_manifest.json
|   |-- .partial/
|   |-- provider_cache/
|   `-- <checkpoint files>
|-- scripts/
|   |-- predict_single_point.py
|   `-- predict_relax.py
|-- jobs/
|   `-- job_status.json
|-- logs/
|-- raw_outputs/
|-- results/
|   |-- adsorption_energy_SP.csv
|   `-- adsorption_energy_Relax.csv
|-- errors/
|   |-- single_point_errors.csv
|   `-- relaxation_errors.csv
|-- structures/
|   `-- (internal per-frame structures are under frames/)
`-- validation/
    |-- single_point_validation.json
    |-- relaxation_validation.json
    |-- script_calculator_isolation.json
    `-- relaxation_integrity.csv
```

Do not mix model files in loose shared directories.

## Environment Policy

Before creating or modifying an environment, first write
`models/<model_key>/environment_preparation.json` using
`assets/environment_preparation.schema.json`. This is a hard gate: no package
installation, environment creation, CUDA toolkit change, or checkpoint load is
allowed before the official guides and host snapshot are recorded.

Inspect existing environments and progress. An environment may have been
created manually, by a previous run, or for a compatible same-family model.

Record in `env_probe.json`:

- Candidate environment names and paths.
- Python executable.
- Required official imports.
- Package versions.
- CUDA/PyTorch availability when relevant.
- Minimal model load/instantiate probe.
- Operating-system, glibc, compiler, scheduler, container/host path mapping,
  network, cache, and filesystem constraints when relevant.

The host snapshot must include, when available, NVIDIA driver and `nvidia-smi`
output, CUDA toolkit/runtime versions, GPU names and compute capability, Python
and pip versions, PyTorch/TensorFlow/JAX CUDA build information, compiler and
glibc versions, filesystem writability, network availability, and all detected
`uv`, `conda`, `mamba`, and agent-runtime candidates. Capture observations, not
launcher intent.

Use the read-only bundled probe as the baseline snapshot and preserve its exact
output before any installation decision:

```text
python scripts/probe_environment.py \
  --output models/<model_key>/environment_preparation.host_snapshot.json
```

Reference that immutable snapshot from `environment_preparation.json`; do not
replace it with a manually asserted CUDA or package version.

Capture each official guide before selecting the toolchain:

```text
python scripts/environment_preparation_control.py capture-guide \
  models/<model_key> --source-url <official-url> --official-host <official-host> \
  --source-path <saved-response> --attested-by user \
  --kind repository_readme --authority official_repository \
  --used-for environment-creation
```

Freeze a derived installation plan only after it references one or more saved
official guides. The plan must contain the official Python constraint, verbatim
commands, pinned packages, environment variables, and official smoke tests:

```text
python scripts/environment_preparation_control.py freeze-plan \
  models/<model_key> --plan-source <installation-plan.json>
```

Then select and validate the toolchain:

```text
python scripts/environment_preparation_control.py select-toolchain \
  models/<model_key> --host-snapshot \
  models/<model_key>/environment_preparation.host_snapshot.json
python scripts/environment_preparation_control.py validate models/<model_key>
```

The controller records `user_decision_pending` when neither uv nor conda/mamba
is available. It never installs uv or silently falls back.

Create the environment only after the preparation record validates. First
inspect the exact plan:

```text
python scripts/environment_install_control.py plan \
  models/<model_key> --environment-path <environment-path>
```

Environment mutation requires an explicit user authorization record:

```text
python scripts/environment_install_control.py authorize \
  models/<model_key> --approved-by user --allow-creation --allow-install
python scripts/environment_install_control.py create \
  models/<model_key> --environment-path <environment-path> --execute
```

For a batch of models, one explicit user confirmation may authorize the whole
frozen plan for that model:

```text
python scripts/environment_install_control.py auto-create \
  models/<model_key> --environment-path <environment-path> --approved-by user
```

`auto-create` is still blocked by missing guides, an invalid installation plan,
toolchain drift, command failure, or failed smoke tests. It does not install uv
or switch package managers.

The executor uses only the frozen toolchain and installation plan. It never
falls back to another package manager, never installs uv implicitly, and writes
`environment_execution.json` with command results and the observed environment
Python probe. It then runs `scripts/gpu_runtime_probe.py` inside the created
environment and requires `nvidia-smi`, the declared framework, a visible GPU,
and a finite GPU tensor operation. A nonzero install, smoke-test, or GPU probe
blocks the model.

After each model task starts inside its selected environment, write
`validation/runtime_provenance.json` using observed runtime values rather than
the launcher's intended values. Completion requires the observed executable,
package versions, task script paths, and hashes to match `model_config.json`.

## Official Model-Usage Evidence

Environment installation guidance does not define model inference. Before
creating either model-owned test script, capture the official usage guide(s):

```text
python scripts/environment_preparation_control.py capture-guide \
  models/<model_key> --guide-scope usage --source-url <official-url> \
  --official-host <official-host> --kind <repository_readme|model_card|package_doc> \
  --authority <official_repository|official_model_card|official_package_docs> \
  --used-for script_generation --used-for smoke_test --used-for runtime_execution
```

Local user-attested guides use `--source-path` and `--attested-by user`. The
guide is stored under `models/<model_key>/resources/official_guides/usage/` with
source URL, revision, retrieval time, authority, verbatim-content flag, and
SHA-256. Freeze and validate the derived model usage plan:

```text
python scripts/model_usage_control.py freeze-usage-plan \
  models/<model_key> --plan-source <model-usage-plan-source.json>
python scripts/model_usage_control.py validate-usage-plan models/<model_key>
```

The plan must cover official imports, checkpoint loading, calculator factory,
device, dtype, units, SP/Relax API, official smoke tests, and benchmark-only
adaptations. Unresolved official version/API/model-variant conflicts block both
script generation and execution. A guide or plan hash change requires a new
script/run identity.

After the official minimal example is actually run, save
`validation/official_usage_smoke_test.json` and validate it. Before registering
model-owned scripts, run:

```text
python scripts/model_usage_control.py validate-official-smoke-test models/<model_key>
python scripts/model_usage_control.py prepare-model-script models/<model_key> \
  --single-point-path models/<model_key>/scripts/predict_single_point.py \
  --relaxation-path models/<model_key>/scripts/predict_relax.py
```

The preparation record binds script hashes to the usage plan, environment,
checkpoint manifest, smoke test, and calculator-isolation evidence.

Select the environment toolchain in this strict order:

1. A user-owned `uv` already available in the requested execution context.
2. An agent-owned `uv` exposed by the active Hermes/OpenClaw-like runtime,
   only if its path, owner, version, and execution context are recorded.
3. Existing `conda` or `mamba`, preserving the same separation and official
   dependency pins.
4. If none is available, stop with `user_decision_pending` and ask whether the
   user authorizes installing `uv`. Never install it silently and never silently
   switch from `uv` to conda or from one agent runtime to another.

Record every checked alternative, including why it was unavailable or rejected.
Do not rename existing environments. Preserve existing names/paths and record
which model keys they serve. Name only new environments by model key, such as
`mlp-<model_key>`, unless the user specifies otherwise.

Default to independent environments. Same-family models may share only when
official requirements are compatible. Never modify a verified environment for a
later model unless the user explicitly accepts the reproducibility risk.

Official model instructions have priority and must be applied literally:

1. Exact frozen model repository from the Matbench snapshot or the user's
   confirmed authoritative identity link.
2. Official model card/checkpoint page.
3. Official package docs.
4. Existing project scripts for the same model family.
5. Minimal glue code for this benchmark's I/O only.

Do not invent dependency versions, install commands, checkpoint names, model
heads, or APIs when official sources provide them. Before running a derived
command, retain the verbatim official command and record the transformation and
reason. If the official guide requires a tool unavailable on the host, block
and ask for a decision instead of substituting a different package manager.

When running on sari06/Hermes, or when a matching error occurs, read
`references/runtime-compatibility.md`. Treat its entries as dated compatibility
evidence, not universal instructions. Re-verify versions before applying a
workaround and record every source patch with a diff/hash and reason.

## Checkpoints

Use resources from the frozen worklist, repaired through Step 2 if needed.
Perform the full checkpoint download in Step 3 after environment probing and
before writing/running the smoke test.

Use exactly this storage layout:

```text
benchmark_root/
`-- models/
    `-- <model_key>/
        `-- checkpoints/
            |-- checkpoint_manifest.json
            |-- .partial/
            |-- provider_cache/
            `-- <checkpoint files>
```

The final checkpoint root is always
`benchmark_root/models/<model_key>/checkpoints/`. Do not place final checkpoints
in another model's directory, a user's global Hugging Face/Torch cache, an
environment directory, the current working directory, or a temporary directory.
Do not use a symlink to an external cache as the only checkpoint copy.

Before any SDK or provider download, redirect its cache variables/configuration
to `models/<model_key>/checkpoints/provider_cache/`. Download incomplete files
under that checkpoint directory's `.partial/` and move them to their final names
only after the download and verification succeed.

Write `checkpoint_manifest.json` using
`assets/checkpoint_manifest.schema.json`, including:

- `model_key`, checkpoint name, revision/version, and source URL/provider.
- Final path relative to `benchmark_root`.
- File size and SHA-256; include an official checksum separately when provided.
- Download time, verification time, access/gated status, and downloader/tool.
- Any provider file ID, ETag, license, or required model-head information.

On resume, reuse an existing checkpoint only when the manifest, file size, and
checksum validate. Otherwise preserve diagnostic information and redownload or
repair it inside the same fixed model directory.

Apply the same ingestion workflow to user-provided checkpoints:

1. Treat the user path as read-only source data; do not move, rename, or modify
   it.
2. Copy the required checkpoint file or directory tree into
   `models/<model_key>/checkpoints/`, preserving required relative filenames.
3. Reject accidental cross-model placement by confirming the destination
   `<model_key>` matches `model_config.json`.
4. Compute and record each copied file's size and SHA-256. For a multi-file
   checkpoint, list every required file in `checkpoint_manifest.json`.
5. Record `source_kind = user_provided` and the original absolute source path
   in the manifest, without recording credentials.
6. Make model scripts load only the verified copied path, never the original
   user path.

## Common Script Interface

Each model may use different internal APIs, but external CLI should be stable:

```text
python predict_single_point.py --config benchmark_config.json \
  --model-config models/<model_key>/model_config.json \
  --output-csv models/<model_key>/results/adsorption_energy_SP.csv \
  --error-csv models/<model_key>/errors/single_point_errors.csv

python predict_relax.py --config benchmark_config.json \
  --model-config models/<model_key>/model_config.json \
  --output-csv models/<model_key>/results/adsorption_energy_Relax.csv \
  --error-csv models/<model_key>/errors/relaxation_errors.csv \
  --frame-root models/<model_key>/frames/relaxation
```

The MACE example
`benchmark_root/calculate_mace_adsorption_energy_true_batch.py` is a reference
for per-frame CSV output, resume behavior, and energy decomposition only. Do not
copy its batching or calculator lifetime blindly. The executable contract is
strictly one structure at a time: the model script receives a single
`structure_index`, writes to `models/<model_key>/frames/<task>/<index>/`, and
commits `frame_manifest.json` only after calculator release and `cleanup.json`
are complete.

Every model must own separate real SP/Relax script files inside its per-model
workspace. Multiple models must not share one test script, executable runner,
symlink, or wrapper. Shared code is limited to stateless I/O/validation helpers;
read the script-isolation reference for the boundary.

Do not treat separate filenames alone as isolation. Reject shared file targets,
symlinks, duplicate script hashes, and model-local files that merely delegate
execution to a shared predictor. Validate the ownership and runtime-provenance
contract before merge.

## Single-Point Script Requirements

The script must read `benchmark_config.json`, preserve frame order, compute every
frame if possible, support resume, and write per-frame CSV results. Results must
not live only in stdout/stderr/Slurm logs.

Create a fresh calculator object for every frame. Never attach one calculator
object to multiple structures. Reuse immutable loaded model weights only when
the provider supports it and the isolation probe records identical A→B/B→A
results, distinct calculator identities, and no mutable state leakage.

Required result columns:

```text
structure_index,slab_composition,E_slab_plus_adsorbate_eV,E_slab_eV,E_adsorbate_eV,E_adsorption_eV,status,error_message
```

Error CSV columns:

```text
structure_index,task,model_key,error_stage,error_type,error_message,traceback_path,retry_count,timestamp
```

## Relaxation Requirements

Relaxed adsorption energy requires both `slab + adsorbate` and clean `slab` to
be relaxed, with the same bottom-layer constraint rule applied to both.

Baseline relaxation settings:

- Primary optimizer: FIRE. Use BFGS only after a concrete FIRE-unsupported
  condition is observed and recorded as a scientific exception.
- VASP-style force criterion: `-0.05`.
- ASE-style implementation: `fmax = 0.05 eV/A`.
- Keep the cell fixed (`optimize_cell = false`).
- After Relax, compare initial and final coordinates for every automatically
  selected fixed bottom-layer atom using MIC. The configured
  `fixed_atom_max_displacement_A` is `0.05 Å`; missing evidence or any larger
  displacement invalidates the frame, even if the model reports convergence.
- Run at most 1000 steps initially. If still unconverged, automatically rerun
  that frame with 5000 maximum steps. Do not treat the 1000-step result as final;
  if the 5000-step rerun also fails to converge, record `unconverged` and exclude
  it from numerical metrics.
- Record optimizer, maximum steps, cell policy, trajectory interval, and
  unconverged-result policy; do not rely on implicit defaults.
- Slabs with 5 layers or fewer: fix bottom 1 layer.
- Slabs with 6 layers or more: fix bottom 2 layers.
- Determine layer count from slab atomic layers, normally z-coordinate
  clustering with recorded tolerance.
- Isolated adsorbate energy is constant across the benchmark and is not
  recomputed per frame unless the user changes the definition.
- Keep the per-frame relaxed extxyz inside the frame artifact directory for
  provenance. Final delivery converts each valid frame to one standard POSCAR
  under `summary/structures/Relax/<model_key>/<structure_index>/POSCAR`; do not
  concatenate multiple POSCAR files into one file.
- Create fresh, separate calculator instances for each frame's
  slab-plus-adsorbate and clean-slab calculations.
- Check adsorbate integrity after every relaxation using the configured
  dataset-specific dissociation rule.
- Save dissociated structures separately and write the per-frame integrity
  audit.

Do not use another optimizer. BFGS is the only allowed fallback, and only when
FIRE is unsupported. Record the exact incompatibility, exception ID, optimizer,
attempt count, and applied maximum steps. Keep the benchmark-wide baseline
visible and do not present fallback results as strictly identical protocols
without a qualification.

Required relaxation CSV columns:

```text
structure_index,slab_composition,E_slab_plus_adsorbate_eV,E_slab_eV,E_adsorbate_eV,E_adsorption_eV,converged,n_steps,fmax_eV_per_A,dissociation_status,dissociation_reason,broken_bond_count,max_monitored_bond_distance_A,monitored_bond_distances_A_json,integrity_check_method,status,error_message,attempt_count,optimizer,optimizer_fallback_reason,max_steps,cell_fixed,slab_layer_count,fixed_layer_count,scientific_exception_ids
```

For every row reported as successful, write finite component energies and enforce
`E_adsorption = E_slab_plus_adsorbate - E_slab - E_adsorbate` within the configured
tolerance. A successful Relax row must have `converged=true` and finite measured
`fmax_eV_per_A` no greater than the configured convergence threshold plus force
tolerance. Write `unconverged` when convergence is not reached; never pair
`status=success` with `converged=false`. Calculation exceptions and nonfinite
energies must be `failed`, with the cause in `error_message` and the error CSV.

If a model cannot relax structures, record why and mark relaxation
blocked/skipped. Ask whether to continue with single-point only when
single-point was not already requested; never add it silently.

## Run Method And Smoke Test

Use the user-specified run method, such as `nohup` or Slurm. If unspecified, ask
before launching long jobs. Avoid heavy full runs on login nodes.

Before submission, resolve container-visible paths to compute-node-visible paths
and reject broken symlinks. Probe the scheduler's accepted resource syntax
instead of assuming `--mem` or GPU GRES behavior.

Before full SP/Relax jobs, run a 2-frame smoke test verifying imports,
checkpoint load from the fixed directory, manifest/checksum validation, config
reads, adsorbate selection, slab composition, sign
convention, CSV/error CSV writing, energy units, model-owned script paths,
fresh-calculator creation, and A/B versus B/A order invariance. For Relax, also
verify dissociation columns and integrity-audit output.
The smoke-test artifact must record the official usage guide paths and hashes,
the frozen usage-plan hash, and an explicit pass for the official minimal usage
example before the benchmark-specific A/B test is accepted.

Sign convention:

```text
E_ads = E_slab_plus_adsorbate - E_slab - E_adsorbate
```

## Submit And Monitor Jobs

Submission does not end the agent's responsibility for a task. After every
full SP or Relax submission, monitor the job to a terminal state without asking
the user to check whether it has finished.

Immediately after submission:

1. Capture the scheduler job ID or local PID from the submission command.
2. Write `models/<model_key>/jobs/<task>_job_status.json` using
   `assets/job_status.schema.json`.
3. Atomically update the matching task checkpoint and central `run_state.json`,
   then append the submission event.
4. Start a monitoring loop. If the job is expected to outlive the current
   interaction, use the available recurring monitor, thread wakeup, or
   equivalent persistent wait mechanism so monitoring resumes automatically.

Record at least:

```json
{
  "schema_version": 4,
  "run_id": "<run_id>",
  "model_key": "<model_key>",
  "task": "single_point",
  "run_method": "slurm",
  "job_id": null,
  "pid": null,
  "state": "queued",
  "submit_time": "<ISO-8601>",
  "last_checked": "<ISO-8601>",
  "exit_code": null,
  "log_path": "<relative-path>",
  "result_path": "<relative-path>",
  "active_indices": [0]
}
```

Monitor using the run method's authoritative signals:

- Slurm: query `squeue` while queued/running, then use `sacct` or scheduler
  accounting for the terminal state and exit code. A job disappearing from
  `squeue` is not by itself evidence of success.
- `nohup` or another local background process: record the PID, check process
  existence, then use an explicit exit-code/status file written by the launcher.
  Process disappearance is not by itself evidence of success.
- Other schedulers: use their queue and accounting commands and record the
  equivalent job identity, terminal state, and exit code.

Use a bounded backoff that avoids excessive polling: check relatively often
while a short job is expected to finish, then reduce frequency for long queued
or running jobs. Send concise progress updates when state changes or at a
reasonable interval, but do not require a user reply for normal queued,
running, or successful states.

For every completed or failed frame, commit in this exact order: persist the
per-frame manifest, result/error, and cleanup artifacts, validate that complete
frame directory, run
`run_control.py record-frame`, update central summaries atomically, and append
the event. Heartbeat the lease every 60 seconds.

On a successful terminal state:

1. Confirm the expected CSV and other task outputs exist.
2. Run the task-specific validation below.
3. Retry only missing or failed frames according to the recovery policy.
4. Mark the task complete only after output validation succeeds.
5. Start the next required task or model automatically.

On a failed, cancelled, preempted, timed-out, or out-of-memory terminal state:

1. Preserve the job state, exit code, logs, and partial outputs.
2. Diagnose the failure and apply safe, benchmark-preserving recovery allowed
   by the retry policy.
3. Resubmit and continue monitoring when recovery is unambiguous.
4. Ask the user only when recovery needs credentials, new resources, a change
   to scientific definitions, or a choice not covered by this skill.

At the start or resumption of Step 3, inspect every recorded nonterminal job
before submitting anything new. Reattach monitoring to a live job; reconcile a
terminal job against its logs and outputs. Never duplicate-submit a task merely
because the earlier agent turn ended.

## Validation And Recovery

Validate SP as soon as SP finishes; validate Relax separately after Relax
finishes.

Required validation:

- CSV exists.
- Every expected structure appears as valid or has a structured failure record.
- Structure indices match benchmark order.
- Slab composition excludes adsorbate.
- Numeric energy columns for successful frames.
- Energy decomposition matches sign convention within tolerance.
- No result exists only in logs.
- Relaxed structures exist or missing structures are explained.
- Each requested task uses the current model's own real script file.
- Calculator isolation validation passes and shows fresh instances per
  structure/component.
- Every readable relaxed structure has `intact`, `dissociated`, or `unknown`
  integrity status with recorded evidence.
- Dissociated structures and the integrity audit exist when Relax was requested.

If frames are missing/failed, recover before advancing:

- Re-run missing/failed frames when resume supports it.
- Diagnose boundary handling, adsorbate selection, memory, timeout, checkpoint
  load, unsupported elements, or model API issues.
- Adjust only benchmark-defined/user-approved runtime settings such as batch
  size, timeout, or job resources.
- Do not change scientific definitions to force success.
- Keep every failed attempt in the error CSV.

Incomplete coverage is acceptable only after recoverable failures are retried,
remaining failures have concrete reasons, and the user approves partial results.

## MLP Timeout, DFT Monitoring, Retry, And Progress

The configured `retry_policy.per_frame_timeout_seconds` is scoped to MLP frame
execution only (`per_frame_timeout_scope=mlp_only`). It must never be passed to
or interpreted by the DFT controller. DFT has no Skill-imposed walltime,
per-calculation timeout, or idle timeout. Do not cancel a DFT job because it is
long-running, temporarily quiet, or still in SCF.

For DFT, run the output monitor and terminate only when a frozen diagnostic
rule matches a fatal process error, confirmed electronic-step oscillation,
corrupted terminal output, explicit user stop, or an external scheduler
termination. Missing or incomplete output while a job is active is not a
failure. Retry transient failures no
more than `retry_policy.max_attempts`. Continue after nonfatal single-frame
failures, but pause and ask for systematic failures such as import failure,
checkpoint-load failure, repeated
CUDA OOM, or many consecutive frame failures.

Update task checkpoints, `run_state.json`, and `events.jsonl` after each frame
transition. Verified frame manifests are authoritative: a completion claim
without a valid frame directory returns to pending, while a valid frame artifact
missing from the checkpoint is reconstructed during reconciliation. Never mark
a model complete when any per-frame result/error, cleanup proof, or Relax final
structure is missing. Keep monitor fields synchronized with the task job
status. Do not ask the user to confirm normal job completion.

## Configuration binding

Apply `references/catalysis-aware-selection.md` before usage-plan freeze. Retain
the same GitHub content hash and commit in an official usage guide; record the
selected execution_variant, assessment hash and exact model-local checkpoint
path. Model-owned scripts inspect initialized provider configuration and write
execution_variant plus calculator_initializations into smoke evidence, each
runtime task record and every completed frame manifest. Validate observations
before inference. The three energy components must not silently use different
task parameters, heads or checkpoints.
