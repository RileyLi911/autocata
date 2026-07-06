# Agent-Ready Workflow

This project can now run the structure-generation pipeline from a single
workflow config, without using an external agent framework.

All user-facing reports should be written in English by default.

The intended future pattern is:

1. A natural-language agent converts a user request into `config/workflow_config.yml`
   or `config/reaction_workflow_config.yml`.
2. The local runner executes the existing CLI tools.
3. The runner writes machine-readable CSV/JSON outputs for the agent to inspect.

Example user request:

```text
I want CH3 as the adsorbate, Au-based catalyst materials, MLP energy < 0,
and 5 successful structures.
```

Once an agent has read `SKILL.md`, `AGENTS.md`, this file, and
`config/adsorbates.yml`, short user prompts are enough. Examples:

```text
Run CH3 screening on unrestricted materials. Use CUDA, E_pred < 0, target 1000.
```

```text
Run CH3 screening on Cu-based catalysts. Use CUDA, E_pred < 0, target 500.
```

```text
For CO2RR, choose available intermediates from the adsorbate registry and run a
reaction-network search for one shared material.
```

Equivalent workflow config fields:

```yaml
workflow:
  adsorbate: "CH3"
  target_success_count: 5
  material_query:
    mode: "contains"
    elements: ["Au"]
  filters:
    require_adsorbate_valid: true
    mlp_energy_lt: 0.0
```

## Runner

Dry-run the planned commands:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml --dry-run
```

Run the MVP workflow:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml
```

Override the adsorbate:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml --adsorbate CH3
```

## Agent Setup Check

Before running workflows on a new machine:

```bash
conda activate autocata
```

If the environment does not exist:

```bash
conda env create -f envs/autocata.yml
conda activate autocata
```

Download or validate the required model assets:

```bash
python script/download_models.py
python script/download_models.py --check-only
```

Then run at least one dry-run:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml --dry-run
```

By default, the workflow first looks up the adsorbate in:

```text
config/adsorbates.yml
```

That registry maps adsorbates to finetuned checkpoints under:

```text
data/finetune_model/checkpoint-{adsorbate}
```

If an adsorbate is not listed in the registry, the runner falls back to:

```text
outputs/finetune_{adsorbate}/checkpoint-*
```

The workflow does not launch training unless:

```yaml
workflow:
  fine_tune:
    enabled: true
```

## Node Order

Each round runs these existing CLI nodes:

```text
script/generate.py
script/ASE_check_manual.py
script/index_xyz_metadata.py
MLP_check/OC20_MLP/mlp_scores.py
```

The runner then filters rows by:

- `adsorbate_valid == true`
- material query match
- `E_pred < workflow.filters.mlp_energy_lt`
- optional duplicate check

## Reaction-Network Runner

For reactions with multiple key intermediates, use the reaction-network runner.
It reuses the single-adsorbate workflow for each adsorbate, then aggregates
accepted structures by catalyst material.

Dry-run:

```bash
python reaction_workflow_runner.py --reaction-config config/reaction_workflow_config.yml --dry-run
```

Run:

```bash
python reaction_workflow_runner.py --reaction-config config/reaction_workflow_config.yml
```

Override adsorbates from the command line:

```bash
python reaction_workflow_runner.py --reaction-config config/reaction_workflow_config.yml --adsorbates CH3,CHO,OH
```

The reaction config controls:

```yaml
reaction_workflow:
  adsorbate_registry: "config/adsorbates.yml"
  adsorbates: ["CH3", "CHO", "OH"]
  target_shared_material_count: 1
  shared_material:
    target_success_per_adsorbate_per_material: 1
```

Each child adsorbate run is written under:

```text
outputs/reaction_workflows/{run_name}/adsorbate_runs/{adsorbate}/
```

The main reaction-level outputs are:

```text
reaction_workflow_report.json
adsorbate_run_summary.csv
all_success_structures.csv
shared_material_summary.csv
material_adsorbate_matrix.csv
reaction_run.log
```

`shared_material_summary.csv` ranks materials by how many target adsorbates
they cover. `material_adsorbate_matrix.csv` is the most useful table for an
agent: it has one row per material and per-adsorbate success counts/best
energies, so the next step can focus on missing adsorbate-material pairs.

## Outputs

Runs are written under:

```text
outputs/workflows/{run_name}/
```

Important outputs:

```text
success_structures/
viewer/index.html
all_candidates.csv
success_summary.csv
failure_summary.csv
workflow_report.json
run.log
rounds/
```

Reaction-network runs are written under:

```text
outputs/reaction_workflows/{run_name}/
```

`workflow_report.json` is the main machine-readable status file.

`all_candidates.csv` contains one row per MLP-scored structure with:

```text
round, xyz_file, sample_id, E_pred, F_rms, F_max,
material, material_elements, adsorbate_valid,
passed, failure_reason, error
```

`viewer/index.html` is a browser-based 3D gallery for accepted structures. It
uses 3Dmol.js and embeds accepted XYZ text into the HTML, so it can be opened
directly from the local filesystem without a separate web server.

On the workflow host, open it directly:

```bash
start outputs/workflows/{run_name}/viewer/index.html
```

To open the viewer automatically after a local workflow run, use:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml --open-viewer
```

or set:

```yaml
workflow:
  viewer:
    auto_open: true
```

## Failure Reasons

The runner currently records:

```text
parse_failed
wrong_adsorbate
wrong_material
mlp_error
mlp_not_passed
duplicate
```

Future geometry checks can add:

```text
geometry_failed
```

## Material Queries

Exact material:

```yaml
material_query:
  mode: "exact"
  material: "Au-Pd"
```

Au-based materials:

```yaml
material_query:
  mode: "contains"
  elements: ["Au"]
```

Any material containing Au or Pd:

```yaml
material_query:
  mode: "any_contains"
  elements: ["Au", "Pd"]
```

No material filter:

```yaml
material_query:
  mode: "any"
```

## Common Short Requests

Broad single-adsorbate production:

```text
Run CH3 production screening on unrestricted materials. Use CUDA, E_pred < 0,
target 1000 accepted structures, and dry-run first.
```

Element-based production:

```text
Run CH3 screening on Au-based catalysts. Use CUDA, E_pred < 0, target 500, and
dry-run first.
```

Exact-material production:

```text
Run CH3 screening on exact Au-Pd catalysts. Use CUDA, E_pred < 0, target 100,
and dry-run first.
```

Reaction-network search:

```text
Run a CO2RR reaction-network search using available registry adsorbates. Find at
least one shared material and report missing adsorbate-material gaps if none are
found.
```

Material names come from `index_xyz_metadata.py`, where element sets are sorted
alphabetically. For example, `Pd-Au` and `Au-Pd` both become `Au-Pd`.

## MVP Limits

- The runner is a local orchestrator, not an external agent framework.
- It does not parse natural language itself.
- It assumes the existing CLI tools are installed and runnable in the current Python environment.
- Fine-tuning remains optional because it is expensive.
- Duplicate checking is byte-level XYZ matching when enabled; structural
  equivalence checks can be added later.
