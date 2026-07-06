# AutoCata Tools

Use this file for local operational notes. Do not store API keys, tokens, or
private credentials here.

## Conda Environment

Recommended environment:

```bash
conda activate autocata
```

Create it when missing:

```bash
conda env create -f envs/autocata.yml
```

Fallback dependency notes are in `envs/autocata_manual.txt`.

## Model Assets

Model assets are configured in:

```text
config/model_assets.yml
```

Download them from Hugging Face Hub:

```bash
python script/download_models.py
```

Validate local assets:

```bash
python script/download_models.py --check-only
```

## Main CLI Entry Points

Single-adsorbate workflow:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml
```

Reaction-network workflow:

```bash
python reaction_workflow_runner.py --reaction-config config/reaction_workflow_config.yml
```

Dry-run first:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml --dry-run
python reaction_workflow_runner.py --reaction-config config/reaction_workflow_config.yml --dry-run
```

## Output Files To Read

Single-adsorbate runs:

```text
workflow_report.json
success_summary.csv
failure_summary.csv
run.log
```

Reaction-network runs:

```text
reaction_workflow_report.json
adsorbate_run_summary.csv
shared_material_summary.csv
material_adsorbate_matrix.csv
reaction_run.log
```
