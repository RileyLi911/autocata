---
name: autocata-workflow
description: Agent-ready AutoCata workflow for adsorbate-conditioned catalyst structure generation, OC20 MLP screening, and multi-adsorbate reaction-network common-material aggregation. Use when Codex needs to set up the AutoCata conda environment from envs/autocata.yml, download required model assets from Hugging Face using script/download_models.py and config/model_assets.yml, convert natural-language catalysis or electrochemical reaction requests into workflow configs, choose available adsorbates from config/adsorbates.yml, run dry-runs or confirmed local workflows, inspect CSV/JSON reports, and summarize accepted structures/material coverage in English.
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

Before changing configs or running commands, read:

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

Before running workflows, activate the environment:

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

## Adsorbate Selection

For natural-language reaction requests:

1. Infer likely adsorbate intermediates from the reaction.
2. Check each candidate against `config/adsorbates.yml`.
3. Only use entries that exist and have `enabled: true`.
4. Report unavailable but relevant intermediates separately.
5. Ask the user to confirm the final adsorbate list before running a real workflow.

Use the registry `name` for model selection and the registry `formula` for
adsorbate validation.

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
outputs/workflows/{run_name}/run.log
```

Report accepted XYZ paths and `viewer/index.html` when structures pass.

## Reaction-Network Workflow

Use this for multiple intermediates where the final goal is a common catalyst
material across all selected adsorbates.

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
outputs/reaction_workflows/{run_name}/shared_material_summary.csv
outputs/reaction_workflows/{run_name}/material_adsorbate_matrix.csv
outputs/reaction_workflows/{run_name}/reaction_run.log
```

Prefer materials where `all_adsorbates_passed == true`. If none exist, explain
which adsorbates are missing for the best partial materials and suggest
increasing `target_success_count`, `max_rounds`, or `generation_per_round`.

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

For reaction-network exploration, a modest next step is:

```yaml
per_adsorbate:
  target_success_count: 5
  max_rounds: 3
  generation_per_round: 20
```

## Result Summary Pattern

For single-adsorbate runs, summarize:

- status and success count
- accepted structures and MLP energies
- failure reasons
- viewer path

For reaction-network runs, summarize:

- per-adsorbate success counts
- shared material count
- materials covering all adsorbates
- best partial materials and missing adsorbates
- paths to CSV/JSON outputs
