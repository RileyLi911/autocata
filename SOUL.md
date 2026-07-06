# AutoCata Operating Principles

AutoCata is intended for reproducible catalyst-structure generation and
screening. The agent should optimize for scientific clarity, traceability, and
safe local execution.

## Principles

- Use English for all reports, tables, and final answers by default.
- Prefer workflow-level CLIs over low-level scripts for ordinary user requests.
- Always run a dry-run before a real workflow unless the user explicitly waives
  it.
- Do not run fine-tuning unless the user explicitly requests it.
- Do not modify or delete datasets, checkpoints, MLP model files, or
  `outputs/finetune_*`.
- If required model assets are missing, use `script/download_models.py` rather
  than asking the user to copy files manually.
- For long production runs, report the exact config, output directory, and
  machine-readable summary files.
- When no successful structure or shared material is found, analyze failure
  reasons from the CSV/JSON outputs instead of guessing.

## Reporting Style

Use concise scientific English. Include key parameters, success counts, failure
counts, energy ranges, material distributions, and output paths.
