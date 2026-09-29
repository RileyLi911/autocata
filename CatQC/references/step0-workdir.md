# Step 0: Initialize Or Resume A v4 Run

Step 0 is the run initialization and recovery control layer. Enter it only after
the request includes dataset, adsorbate, task selection, and a frozen model
selection from `request-model-selection.md`.
Freeze `dft_reference_mode` as `provided` or `generate_vasp` before matching a
run; changing it changes request identity.

## Confirm One Path

Ask the user only for `workspace_base`. Construct the fixed run collection:

```text
<workspace_base>/CatQC/benchmark_runs/
```

Do not ask them to approve the derived final path and never infer the prefix
from the skill directory, current directory, or dataset path.

## Initialize Or Match

Run:

```text
python scripts/run_control.py init-or-resume \
  --workspace-base <workspace_base> --skill-root <skill_root> \
  --selection-file <frozen-selection.json> --dataset-name <dataset> \
  --dataset-identity <stable-dataset-id-version-or-hash> \
  --adsorbate <adsorbate> --dft-reference-mode <provided|generate_vasp> \
  --tasks <single_point|relaxation ...> \
  --owner-id <stable-controller-id> --original-prompt <request>
```

The request SHA-256 includes the stable dataset identity (name plus version or
content hash when available), adsorbate, requested tasks, DFT reference mode, and
the ordered model identities and versions. Model count alone is never a match.

- One exact incomplete match: acquire its lease, reconcile it, and resume.
- Multiple exact incomplete matches: exit without choosing; show each candidate
  path, aggregate progress, and update time, then ask the user to select one and
  rerun with `--select-run`.
- No exact incomplete match: create a new run.
- `--new-run`: always create a new run.
- Completed runs are never reused.

The directory name is:

```text
YYYYMMDD-HHMMSS_<N>models_<adsorbate_slug>_<sp|relax|sp-relax>/
```

Append `-02`, `-03`, and so on for same-second collisions. Step 1 may block an
adsorbate mismatch but must not rename this directory.

## v4 Layout And Ownership

```text
benchmark_root/
|-- run_manifest.json
|-- models_manifest.json
|-- run_state.json
|-- events.jsonl
|-- control/lease.json
|-- control/latest_reconciliation.json
|-- selection_evidence/
|-- dft/
|-- models/<model_key>/
|   |-- state/single_point_checkpoint.json
|   |-- state/relaxation_checkpoint.json
|   |-- jobs/
|   |-- results/
|   |-- errors/
|   `-- frames/<task>/<structure_index>/
|       |-- input/
|       |-- work/
|       |-- outputs/
|       |-- errors/
|       |-- cleanup.json
|       `-- frame_manifest.json
|-- logs/
`-- summary/
```

`run_manifest.json` freezes run identity and hashes. `models_manifest.json` is
immutable after creation. `run_state.json` is the central summary.
`events.jsonl` is append-only. Each task checkpoint owns frame-level progress.
All mutable JSON is written through a temporary file and atomic replacement.

## Lease And Recovery

Only the owner in `control/lease.json` may mutate central state or submit jobs.
Heartbeat every 60 seconds. The lease expires after 15 minutes. A valid foreign
lease blocks a second controller. After expiry, a controller may take over, but
submission remains disabled until scheduler/PID state has been checked.

Recovery performs these operations in order:

1. Validate v4 manifests and their SHA-256 links.
2. Acquire or take over the lease.
3. Query recorded Slurm job IDs, PIDs, or provider status files.
4. Scan task results, errors, checkpoints, and job statuses.
5. Treat validated result rows as completion truth.
6. Remove false checkpoint completions and rebuild missing checkpoint updates.
7. Reattach monitoring to active jobs and prohibit duplicate submissions.
8. Write `control/latest_reconciliation.json` and continue at the first pending
   model/task/frame.

Use `run_control.py reconcile ... --external-jobs-reconciled` only after the
external job check is genuinely complete. Never use the flag merely to bypass a
takeover gate.

v2 and v3 directories are incompatible. `validate_run.py` must reject them with
an instruction to start a new v4 run. Do not migrate or silently modify them.

## CatQC identity boundary

Only skill_name=catqc manifests at their recorded direct child path under
<workspace_base>/CatQC/benchmark_runs may be resumed. Foreign or missing skill
identities are incompatible, even if their request hashes match. CatQC never
rewrites legacy runs; use their original skill release to resume them.
