# AutoCata Bootstrap

Use this file when an agent opens the repository for the first time.

## First Steps

1. Work from the `autocata/` directory.
2. Read `SKILL.md`, `AGENTS.md`, `README.md`, `README_workflow.md`, and
   `config/adsorbates.yml`.
3. Use English for all user-facing responses unless the user explicitly asks
   for another language.
4. Check that the Conda environment is available:

```bash
conda activate autocata
```

If the environment is missing and setup is requested, create it:

```bash
conda env create -f envs/autocata.yml
conda activate autocata
```

5. Check local model assets:

```bash
python script/download_models.py --check-only
```

If required model assets are missing, download them:

```bash
python script/download_models.py
```

6. Run a dry-run smoke test before any real workflow:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml --dry-run
```

## After Bootstrap

If the current agent session retains context, users can use short prompts such
as:

```text
Run CH3 screening on Cu-based catalysts. Use CUDA, E_pred < 0, target 500.
```

Preserve the safety rules in `AGENTS.md` even when the user's prompt is short.
