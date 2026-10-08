---
name: autocata-workflow
description: Operate AutoCata catalyst structure generation, OC20 screening, reaction-network shared-material searches, and independent MACE or CHGNet scoring of existing XYZ results. Use for workflow configuration, environment/model setup, dry-runs, requested calculations, and interpretation of CSV/JSON reports. MACE/CHGNet scoring produces total energies and forces; it does not replace OC20 adsorption-energy filtering.
---

# AutoCata Workflow

Operate from the directory that contains this file:

```bash
cd autocata
```

If the current working directory is already `autocata/`, stay there.

Use this skill to drive the local AutoCata CLI tools. Do not add an external
agent framework inside the repo.

## Response Language

Respond to the user in English by default. All workflow summaries, result
tables, failure analyses, and final reports should be in concise scientific
English unless the user explicitly requests another language.

## Required Reading

Read [AGENTS.md](AGENTS.md), then route by the requested task:

- Existing-structure MACE/CHGNet scoring or setup: read
  [MLP_check/README.md](MLP_check/README.md), the selected
  [MACE config](config/mlp_mace.yml) or [CHGNet config](config/mlp_chgnet.yml),
  and its environment file under `envs/`. Follow **Independent MLP Scoring**
  below; generation-model assets and the adsorbate registry are not required.
- Structure generation or OC20 workflow screening: read:

```text
AGENTS.md
README_workflow.md
config/adsorbates.yml
```

Also read the relevant config:

```text
config/workflow_config.yml
config/reaction_workflow_config.yml
config/model_assets.yml
```

Read `README_config.md` when debugging training, generation, XYZ conversion, or
MLP scoring internals.

After these files have been read in a retained agent context, users can issue
short requests. Re-read the files only when the session is new, the workspace
has changed, or the request conflicts with remembered rules.

## Environment Bootstrap

This section applies to generation/OC20 workflows. Independent MACE/CHGNet
scoring uses the separate environments described below; do not download
generation or OC20 assets for that task.

Before running generation/OC20 workflows, activate the environment:

```bash
conda activate autocata
```

If the environment is missing and the user has asked to set up or test the
workflow, create it from:

```bash
conda env create -f envs/autocata.yml
conda activate autocata
```

If dependency solving fails, follow `envs/autocata_manual.txt`. After setup,
run a dry-run smoke test before any real workflow.

If required model assets are missing, download them before the dry-run:

```bash
python script/download_models.py
```

To validate assets without downloading:

```bash
python script/download_models.py --check-only
```

## Safety Rules

Do not delete, move, rename, or overwrite:

```text
data/
checkpoint-GPT-2M/
outputs/finetune_*/
MLP_check/OC20_MLP/MLP_model/
```

Do not run fine-tuning unless the user explicitly requests it. Keep:

```yaml
fine_tune:
  enabled: false
```

Run dry-runs before real workflows unless the user has already confirmed the
exact config.

For new users, always dry-run first even when their prompt is short.
Use a unique `workflow.run_name` for production runs. Do not reuse an existing
non-empty run directory unless the user explicitly asks to overwrite or debug
it.
Use native energy-window filters when requested: `mlp_energy_gt` is the lower
bound and `mlp_energy_lt` is the upper bound. For `-1.5 < E_pred < -0.5`, set
`mlp_energy_gt: -1.5` and `mlp_energy_lt: -0.5`; do not rely on post-filtering
after a one-sided workflow.

## Adsorbate Selection

For natural-language reaction requests:

1. Infer likely adsorbate intermediates from the reaction.
2. Check each candidate against `config/adsorbates.yml`.
3. Only use entries that exist and have `enabled: true`.
4. Report unavailable but relevant intermediates separately.
5. Ask the user to confirm the final adsorbate list before running a real workflow.

Use the registry `name` for model selection and the registry `formula` for
adsorbate validation.

## Independent MLP Scoring

Use this mode for requests such as "Score the existing CH3 structures with
MACE" or "Compare MACE and CHGNet on these XYZ files." Use
`MLP_check/score_structures.py` directly, without invoking either workflow
runner. Setup-only, documentation-only, and dry-run-only requests do not imply
permission to run inference. If calculation is already requested, carry it
out after validating the dry-run without asking for redundant confirmation.

1. Select the requested backend and existing `success_summary.csv`,
   `all_success_structures.csv`, or XYZ directory. The example configs contain
   a particular restored run: replace its input/output paths with the user's
   actual targets. For model comparisons, use the same structures for both.
2. Use `autocata-mace` / `envs/mlp_mace.yml` or
   `autocata-chgnet` / `envs/mlp_chgnet.yml`. Keep the existing `autocata`
   environment intact. A path-only dry-run needs no installed model package;
   see the detailed guide for installation when setup or inference is in scope.
3. Select a fresh output directory, typically
   `<existing_run>/mlp_comparison/<backend>_<unique_run>/`. Default to CPU and
   five structures unless the user specifies a different scope. Full-dataset
   scoring requires `--all-files`; do not silently truncate a request for all
   structures to the default five.
4. Run `python MLP_check/score_structures.py --config config/mlp_mace.yml --dry-run`
   (or `config/mlp_chgnet.yml`), with the selected paths overridden as needed.
   Check the selected count, input paths, model and output directory. A dry-run
   checks configuration/paths, not checkpoint compatibility or model accuracy.
5. For requested inference, run the same command without `--dry-run`. Read
   `scores.csv` and `scoring_report.json`, including failures. Report backend,
   model, units, selected/succeeded/failed counts, PBC and output paths.

Essential interpretation rules:

- Outputs are `E_total_eV`, `E_per_atom_eV`, `F_rms_eV_A`, and `F_max_eV_A`.
  They are not the existing workflow's `E_pred`; do not apply its energy
  thresholds, label totals as adsorption energies, or rank different
  compositions by their raw total energies.
- The workflow runners still use OC20. A request to replace workflow screening
  or calculate adsorption energies needs a defined reference-energy scheme
  and additional implementation; do not invent a backend config switch.
- Preserve PBC by default. CHGNet requires a full 3D-periodic vacuum cell for
  slabs; do not silently change PBC to bypass a failure. See the guide for
  model-specific limitations and explicit overrides.
- Retain input XYZ and prior results. Distinguish dry-run validation, mock
  tests and actual model inference when reporting what has been verified.

## Single-Adsorbate Workflow

Use `config/workflow_config.yml`.

Dry-run:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml --dry-run
```

Run after confirmation:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml
```

Override the adsorbate when useful:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml --adsorbate CH3
```

Read and summarize:

```text
outputs/workflows/{run_name}/workflow_report.json
outputs/workflows/{run_name}/success_summary.csv
outputs/workflows/{run_name}/failure_summary.csv
outputs/workflows/{run_name}/all_candidates.csv
outputs/workflows/{run_name}/run.log
outputs/workflows/{run_name}/structure_previews/preview_manifest.json
```

Report accepted XYZ paths and `viewer/index.html` when structures pass. On an
attachment-capable chat channel, read `preview_manifest.json` and attach the
first successful GIF previews. Use the corresponding PNG files when GIF is not
supported. Keep the attachment count concise.
Use `workflow_report.json` and CSV files for round/candidate counts; do not
count stale `rounds/` subdirectories.

## Reaction-Network Workflow

Use this for multiple intermediates where the final goal is a common catalyst
material across all selected adsorbates.

Prefer `reaction_workflow.screening_mode: "material_first"` for production
reaction-network searches. In this mode, AutoCata first generates and parses
large candidate sets, finds materials shared across all selected adsorbates,
and only then runs MLP on those shared-material candidates. This is more
efficient than scoring every generated structure before checking material
overlap.

Production reaction-network searches require large pre-MLP sampling. Unless
the user explicitly says "smoke test", "debug", or gives a smaller fixed
budget, do not use small settings such as `max_rounds <= 5` or
`generation_per_round <= 1000`. Start with at least 100,000 generated
structures per adsorbate:

```yaml
per_adsorbate:
  max_rounds: 10
  generation_per_round: 10000
```

If no pre-MLP shared materials or post-MLP shared materials are found, report
that the generation budget was exhausted; do not imply that the chemistry is
invalid after only hundreds or a few thousand generated structures per
adsorbate.

Edit `config/reaction_workflow_config.yml`, then dry-run:

```bash
python reaction_workflow_runner.py --reaction-config config/reaction_workflow_config.yml --dry-run
```

Run after confirmation:

```bash
python reaction_workflow_runner.py --reaction-config config/reaction_workflow_config.yml
```

Useful override:

```bash
python reaction_workflow_runner.py --reaction-config config/reaction_workflow_config.yml --adsorbates NH,NH2
```

Read and summarize:

```text
outputs/reaction_workflows/{run_name}/reaction_workflow_report.json
outputs/reaction_workflows/{run_name}/adsorbate_run_summary.csv
outputs/reaction_workflows/{run_name}/pre_mlp_shared_material_summary.csv
outputs/reaction_workflows/{run_name}/targeted_mlp_inputs.csv
outputs/reaction_workflows/{run_name}/all_mlp_scored_candidates.csv
outputs/reaction_workflows/{run_name}/shared_material_summary.csv
outputs/reaction_workflows/{run_name}/material_adsorbate_matrix.csv
outputs/reaction_workflows/{run_name}/reaction_run.log
outputs/reaction_workflows/{run_name}/structure_previews/preview_manifest.json
```

Prefer materials where `all_adsorbates_passed == true`. If none exist, explain
which adsorbates are missing for the best partial materials and suggest
increasing `target_success_count`, `max_rounds`, or `generation_per_round`.
In `material_first` mode, distinguish between pre-MLP shared candidates and
post-MLP shared materials: do not call a material successful unless it appears
in `shared_material_summary.csv` with `all_adsorbates_passed == true`.
For `material_first`, generation scale is controlled by
`per_adsorbate.max_rounds * per_adsorbate.generation_per_round`;
`per_adsorbate.target_success_count` is mainly for the legacy `mlp_first` mode.

## Config Defaults For Small Tests

Use CPU unless the user asks for GPU:

```yaml
device:
  generation: "cpu"
  mlp: "cpu"
```

Start small:

```yaml
target_success_count: 1
max_rounds: 1
generation_per_round: 5
```

For reaction-network production, start with a large material-first pre-MLP
generation budget:

```yaml
per_adsorbate:
  target_success_count: 5
  max_rounds: 10
  generation_per_round: 10000
material_first:
  target_shared_material_candidates: 100
  max_shared_materials_for_mlp: 100
  max_structures_per_adsorbate_per_material: 50
```

Use smaller settings only when the user explicitly asks for a smoke test or
debug run.

## Result Summary Pattern

For single-adsorbate runs, summarize:

- status and success count
- accepted structures and MLP energies
- applied energy bounds
- failure reasons
- viewer path

For reaction-network runs, summarize:

- per-adsorbate success counts
- shared material count
- materials covering all adsorbates
- best partial materials and missing adsorbates
- paths to CSV/JSON outputs
