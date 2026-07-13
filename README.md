# AutoCata

AutoCata is a local, agent-ready workflow for adsorbate-conditioned catalyst
structure generation and screening. It organizes the internal AutoCata core
scripts, XYZ parsing, material/adsorbate indexing, OC20 MLP scoring, and
multi-adsorbate reaction-network aggregation behind CLI entry points that an
agent can call safely.

The project currently supports:

- config-driven fine-tuning with `train_local.py`
- single-adsorbate generation and screening with `workflow_runner.py`
- registry-based checkpoint lookup from `config/adsorbates.yml`
- multi-adsorbate reaction-network screening with `reaction_workflow_runner.py`
- common-material aggregation through `material_adsorbate_matrix.csv`
- desktop 3D viewing of accepted structures through `viewer/index.html`
- headless PNG and rotating GIF previews that can be attached to chat results

## Default Agent Behavior

Agents using AutoCata should use English for all user-facing responses,
workflow summaries, tables, and final reports unless the user explicitly asks
for another language.

For a fresh agent session, first read:

```text
SKILL.md
AGENTS.md
README_workflow.md
config/adsorbates.yml
config/model_assets.yml
```

If the `autocata` Conda environment is missing, create it from:

```bash
conda env create -f envs/autocata.yml
conda activate autocata
```

If model assets are missing, download them with:

```bash
python script/download_models.py
```

After this one-time bootstrap succeeds, users can give short natural-language
requests such as:

```text
Run CH3 screening on Cu-based catalysts. Use CUDA, E_pred < 0, target 500
accepted structures, and dry-run first.
```

Energy windows are supported directly:

```text
Run CH3 screening on unrestricted materials. Use CUDA, -1.5 < E_pred < -0.5,
target 1000 accepted structures, and dry-run first.
```

## Repository Layout

```text
config/
  config.yml                    base training/generation config
  workflow_config.yml           single-adsorbate workflow config
  reaction_workflow_config.yml  multi-adsorbate reaction workflow config
  adsorbates.yml                available adsorbate checkpoint registry
  model_assets.yml              Hugging Face model asset manifest
script/
  download_models.py            download model assets from Hugging Face Hub
  generate.py                   generate structure token sequences
  ASE_check_manual.py           decode generated pkl into xyz structures
  index_xyz_metadata.py         check adsorbate formula and catalyst material
  build_structure_gallery.py    build desktop 3D viewer
  render_xyz_preview.py         render server-side PNG/GIF previews
MLP_check/OC20_MLP/
  mlp_scores.py                 score xyz structures with OC20 MLP
workflow_runner.py              single-adsorbate workflow runner
reaction_workflow_runner.py     multi-adsorbate/common-material runner
AGENTS.md                       operating guide for agents
SKILL.md                        Codex/OpenClaw skill instructions
MODEL_DOWNLOAD.md               model asset download/release instructions
README_workflow.md              detailed workflow documentation
README_config.md                training/generation config documentation
```

## Local Assets

Large model, checkpoint, generated output, and private dataset files are local
artifacts. They should usually not be committed to GitHub.

Expected local paths:

```text
checkpoint-GPT-2M/checkpoint-138890/
data/finetune_model/checkpoint-{adsorbate}/
MLP_check/OC20_MLP/MLP_model/*.pt
outputs/
```

For public distribution, keep model assets on Hugging Face Hub rather than in
normal Git history. The default asset manifest is:

```text
config/model_assets.yml
```

Download assets with:

```bash
python script/download_models.py
```

See `MODEL_DOWNLOAD.md` for Hugging Face upload/download instructions and the
optional GitHub Release fallback.

`config/adsorbates.yml` records which finetuned adsorbate checkpoints are
available locally. Each entry maps an adsorbate name, formula, and enabled flag
to:

```text
data/finetune_model/checkpoint-{adsorbate}
```

## Environment

Clone the code repository, then create and activate the recommended Conda
environment:

```bash
git clone https://github.com/RileyLi911/autocata.git
cd autocata
conda env create -f envs/autocata.yml
conda activate autocata
```

See `envs/autocata_manual.txt` for fallback install steps if PyG or fairchem
dependencies need manual installation.

When an agent is asked to set up AutoCata on a new machine, it should check for
the `autocata` environment first. If it is absent, the agent may create it from
`envs/autocata.yml`, then run the dry-run checks below.

Download or validate the model assets:

```bash
python script/download_models.py
python script/download_models.py --check-only
```

If the assets are stored in a different Hugging Face repository, override the
repo id:

```bash
python script/download_models.py --repo-id owner/autocata-models
```

## Single-Adsorbate Workflow

Dry-run:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml --dry-run
```

Run:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml
```

Override the adsorbate:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml --adsorbate CH3
```

Important outputs:

```text
outputs/workflows/{run_name}/
  success_structures/
  all_candidates.csv
  success_summary.csv
  failure_summary.csv
  workflow_report.json
  run.log
  viewer/index.html
  structure_previews/preview_summary.csv
  structure_previews/preview_manifest.json
  structure_previews/*.png
  structure_previews/*.gif
```

## Reaction-Network Workflow

Use this when several adsorbate intermediates should be screened against the
same catalyst material set.

The recommended reaction-network mode is `material_first`: AutoCata first
generates and parses structures for each adsorbate, finds catalyst materials
that appear across all selected adsorbates, and only then runs MLP on those
shared-material candidates.

For production reaction-network searches, use a large pre-MLP generation
budget. Unless the request is explicitly a smoke test or debug run, start with
at least 100,000 generated structures per adsorbate:

```yaml
per_adsorbate:
  max_rounds: 10
  generation_per_round: 10000
material_first:
  target_shared_material_candidates: 100
  max_shared_materials_for_mlp: 100
  max_structures_per_adsorbate_per_material: 50
```

Runs with only hundreds of generated structures per adsorbate are undersampled
and should not be interpreted as evidence that no shared catalyst materials
exist.

Dry-run:

```bash
python reaction_workflow_runner.py --reaction-config config/reaction_workflow_config.yml --dry-run
```

Run:

```bash
python reaction_workflow_runner.py --reaction-config config/reaction_workflow_config.yml
```

Override adsorbates:

```bash
python reaction_workflow_runner.py --reaction-config config/reaction_workflow_config.yml --adsorbates NH,NH2
```

Important outputs:

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

`pre_mlp_shared_material_summary.csv` records shared-material candidates before
MLP. `material_adsorbate_matrix.csv` is the key post-MLP table for
common-material screening: it records, for each material, how many accepted
structures exist for each target adsorbate and whether all target adsorbates
passed.

## Agent Usage

This repository is designed to be called by an external agent such as OpenClaw,
without embedding agent framework code inside the project.

Recommended agent bootstrap:

```text
Read SKILL.md, AGENTS.md, README_workflow.md, config/adsorbates.yml, and the
relevant workflow config before editing files or running commands. Read
config/model_assets.yml before model setup. Respond in English by default.
```

For natural-language reaction requests, the agent should:

1. infer likely adsorbate intermediates;
2. check those names against `config/adsorbates.yml`;
3. ask the user to confirm the final adsorbate list;
4. update `config/reaction_workflow_config.yml`;
5. run a dry-run first;
6. run the real workflow only after confirmation.

If the agent has already completed this bootstrap in the current context, the
user does not need to repeat all file-reading instructions. Short requests are
acceptable as long as the agent preserves the AutoCata operating rules.

## Safety Notes

- Fine-tuning is expensive and should stay disabled unless explicitly requested.
- Do not commit private datasets, checkpoints, MLP model files, or generated
  outputs to GitHub.
- If model assets are missing, run `python script/download_models.py` before
  workflow dry-runs.
- The current generation model is adsorbate-conditioned, not
  material-conditioned. Exact shared-material discovery is therefore based on
  generation plus filtering/aggregation.
- Desktop 3D visualization is generated as `viewer/index.html`. Server-side
  PNG/GIF previews are generated under `structure_previews/` for chat clients
  such as Feishu. AutoCata creates the media files; the calling agent is
  responsible for attaching them to its reply.

## More Documentation

- `README_config.md`: config-driven training, generation, XYZ conversion, MLP
  scoring details
- `README_workflow.md`: single-adsorbate and reaction-network workflow details
- `AGENTS.md`: practical rules for agent operation
- `SKILL.md`: compact instructions for external agents
- `MODEL_DOWNLOAD.md`: model asset packaging and release download guide
