# AutoCata Workflow Agent Guide

This project is being prepared for agent-driven structure generation workflows.
Agents should operate through the workflow layer, not by directly editing model
or checkpoint files.

## Response Language

Use English for all user-facing responses, workflow summaries, tables, and
final reports by default. Use another language only when the user explicitly
requests it.

## Project Goal

Given a user request such as:

```text
I want CH3 as the adsorbate, Au-based catalyst materials, MLP energy < 0,
and 5 successful structures.
```

an agent should convert the request into `config/workflow_config.yml`, run the
local workflow, then summarize the generated results.

## Safe Entry Points

Use the project Conda environment before running workflow commands:

```bash
conda activate autocata
```

If the `autocata` environment is missing during setup or a first-run smoke
test, create it from the environment file:

```bash
conda env create -f envs/autocata.yml
conda activate autocata
```

If the solve or PyG installation fails, follow `envs/autocata_manual.txt`.

If model assets are missing, download them from the configured Hugging Face Hub
repository before workflow dry-runs:

```bash
python script/download_models.py
```

Validate local asset paths with:

```bash
python script/download_models.py --check-only
```

Preferred single-adsorbate entry point:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml
```

Preferred multi-adsorbate reaction-network entry point:

```bash
python reaction_workflow_runner.py --reaction-config config/reaction_workflow_config.yml
```

First test with:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml --dry-run
python reaction_workflow_runner.py --reaction-config config/reaction_workflow_config.yml --dry-run
```

Dry-runs use timestamped dry-run output directories when `workflow.run_name` is
set. For real runs, do not reuse an existing non-empty run directory unless the
user explicitly asks to overwrite or debug it. Prefer a unique `workflow.run_name`
for every production run.

## Session Bootstrap

For a fresh agent context, read these files before editing configs or running
commands:

```text
SKILL.md
AGENTS.md
README.md
README_workflow.md
config/adsorbates.yml
config/model_assets.yml
```

Also read `config/workflow_config.yml` for single-adsorbate requests and
`config/reaction_workflow_config.yml` for reaction-network requests.

After the bootstrap is done in a retained context, the user can send short
requests such as "Run CH3 screening on Cu-based catalysts." Do not require the
user to repeat the file-reading instructions every time; preserve these rules
unless the workspace or session has changed.

The runner orchestrates these lower-level tools:

```text
script/generate.py
script/ASE_check_manual.py
script/index_xyz_metadata.py
MLP_check/OC20_MLP/mlp_scores.py
```

Agents should not call those lower-level tools directly unless the user asks
for debugging a specific workflow node.

## Files Agents May Edit

Agents may edit:

```text
config/workflow_config.yml
config/reaction_workflow_config.yml
config/adsorbates.yml
config/model_assets.yml
README_workflow.md
README_config.md
AGENTS.md
```

Agents may create new workflow output directories under:

```text
outputs/workflows/
outputs/reaction_workflows/
```

## Files Agents Must Not Modify Directly

Do not delete, move, rename, or overwrite:

```text
data/
checkpoint-GPT-2M/
outputs/finetune_*/
MLP_check/OC20_MLP/MLP_model/
```

Do not modify generated checkpoints, tokenizer files, adapter files, or dataset
CSV files unless the user explicitly asks.

## Fine-Tuning Policy

Fine-tuning is expensive and should not run automatically.

Default workflow behavior:

```yaml
workflow:
  fine_tune:
    enabled: false
    require_existing_checkpoint: true
```

Only set `fine_tune.enabled: true` when the user explicitly requests training
or confirms that a new finetuned checkpoint should be created.

## Workflow Config Fields

Important fields in `config/workflow_config.yml`:

```yaml
workflow:
  adsorbate: "CH3"
  target_success_count: 5
  max_rounds: 5
  generation_per_round: 100
  device:
    generation: "cpu"
    mlp: "cpu"
  material_query:
    mode: "contains"
    elements: ["Au"]
  filters:
    require_adsorbate_valid: true
    mlp_energy_gt: null
    mlp_energy_lt: 0.0
```

Energy-window requests should be represented natively:

```yaml
filters:
  mlp_energy_gt: -1.5
  mlp_energy_lt: -0.5
```

Do not satisfy an energy-window request by running only `mlp_energy_lt` and
post-filtering `success_summary.csv`; the runner should keep generating until
the accepted structures already satisfy both bounds.

Important fields in `config/reaction_workflow_config.yml`:

```yaml
reaction_workflow:
  screening_mode: "material_first"
  adsorbate_registry: "config/adsorbates.yml"
  adsorbates: ["CH3", "CHO", "OH"]
  target_shared_material_count: 1
  shared_material:
    target_success_per_adsorbate_per_material: 1
  material_first:
    target_shared_material_candidates: null
    min_candidates_per_adsorbate_per_material: 1
    max_shared_materials_for_mlp: 50
    max_structures_per_adsorbate_per_material: 20
```

In `material_first` mode, the pre-MLP generation budget is
`per_adsorbate.max_rounds * per_adsorbate.generation_per_round`.
`per_adsorbate.target_success_count` is mainly for the legacy `mlp_first` mode.

Production reaction-network searches must use large pre-MLP sampling. Unless
the user explicitly requests a smoke test, debug run, or a smaller fixed
budget, configure at least 100,000 generated structures per adsorbate:

```yaml
per_adsorbate:
  max_rounds: 10
  generation_per_round: 10000
material_first:
  target_shared_material_candidates: 100
  max_shared_materials_for_mlp: 100
  max_structures_per_adsorbate_per_material: 50
```

Do not conclude that no useful shared materials exist after only hundreds or a
few thousand generated structures per adsorbate. Report such runs as
undersampled.

Material query modes:

```text
any          no material filtering
exact        material must exactly match, e.g. Au-Pd
contains     material must contain all listed elements, e.g. Au-based
any_contains material must contain at least one listed element
```

## Recommended Agent Flow

1. Parse the user request into structured fields.
2. Check whether the requested adsorbate exists in `config/adsorbates.yml`.
3. Update `config/workflow_config.yml`.
4. Run a dry-run first.
5. If dry-run looks correct, run the workflow.
6. Read `workflow_report.json`.
7. Read `success_summary.csv` and `failure_summary.csv`.
8. Summarize success count, structure paths, MLP energy range, and failure reasons.

For reaction-network requests with multiple intermediates, update
`config/reaction_workflow_config.yml` instead. Prefer `screening_mode:
"material_first"` so the runner first identifies shared materials from parsed
XYZ metadata, then runs MLP only on those shared-material candidates. After the
run, read `reaction_workflow_report.json`,
`pre_mlp_shared_material_summary.csv`, `all_mlp_scored_candidates.csv`,
`shared_material_summary.csv`, and `material_adsorbate_matrix.csv`. Prefer
materials where
`all_adsorbates_passed == true`; otherwise report the missing adsorbates per
material and recommend targeted follow-up runs.

## Output Files

Each workflow run writes:

```text
outputs/workflows/{run_name}/
  success_structures/
  viewer/
  all_candidates.csv
  success_summary.csv
  failure_summary.csv
  workflow_report.json
  run.log
  rounds/
```

Each reaction-network run writes:

```text
outputs/reaction_workflows/{run_name}/
  adsorbate_runs/
  configs/
  adsorbate_run_summary.csv
  pre_mlp_candidates.csv
  pre_mlp_shared_material_summary.csv
  material_adsorbate_precheck_matrix.csv
  targeted_mlp_inputs.csv
  all_mlp_scored_candidates.csv
  all_success_structures.csv
  shared_material_summary.csv
  material_adsorbate_matrix.csv
  failure_summary.csv
  reaction_workflow_report.json
  reaction_run.log
```

The most important machine-readable file is:

```text
workflow_report.json
```

Use `success_summary.csv` to list accepted structures.
Use `failure_summary.csv` to explain why candidates failed.
Use `all_candidates.csv` and `workflow_report.json` for candidate counts and
round counts. Do not infer the number of completed rounds by counting
directories under `rounds/`, because stale directories can remain from old runs
if an existing run directory was reused.
Use `viewer/index.html` for browser-based 3D visualization of accepted XYZ files.
For local desktop runs, `workflow_runner.py --open-viewer` may be used to open
the viewer automatically after the workflow finishes.
When reporting workflow results, do not stop at a text table. Always report the
local 3D viewer path when accepted structures are available.

## Failure Reasons

Current failure reasons:

```text
parse_failed
wrong_adsorbate
wrong_material
mlp_error
mlp_energy_below_min
mlp_energy_above_max
duplicate
```

Suggested user-facing interpretation:

```text
parse_failed      generated sequence could not be converted to a structure
wrong_adsorbate   final atoms did not match the requested adsorbate formula
wrong_material    catalyst material did not match the material query
mlp_error         MLP calculation failed
mlp_energy_below_min predicted energy was not greater than `mlp_energy_gt`
mlp_energy_above_max predicted energy was not less than `mlp_energy_lt`
duplicate         structure matched a previously accepted structure
```

## Safety Defaults

Use CPU unless the user explicitly asks for GPU:

```yaml
device:
  generation: "cpu"
  mlp: "cpu"
```

Use small test settings for first runs:

```yaml
target_success_count: 1
max_rounds: 1
generation_per_round: 5
```

Increase generation scale only after the small workflow succeeds.

## Do Not

Do not connect external services or agent frameworks from inside this repo.
Do not add OpenClaw, LangGraph, AutoGen, or similar runtime code unless the user
explicitly requests it.
Do not store API keys or credentials in this repository.
Do not rewrite lower-level model code for ordinary workflow requests.
