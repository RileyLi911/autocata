# Configuration Usage

Training is driven by `config/config.yml` plus optional CLI overrides.

For agent-facing use, prefer the workflow runners documented in
`README_workflow.md`. Direct training or low-level CLI calls should be used only
for debugging or explicit fine-tuning requests.

All user-facing reports should be written in English by default.

Basic usage:

```bash
python train_local.py --config config/config.yml
```

Choose an adsorbate from the command line:

```bash
python train_local.py --config config/config.yml --adsorbate CH2CH3
```

`experiment.adsorbate` is used to fill these templates:

- `experiment.name_template` -> `model_params.name`
- `paths.train_data_template` -> `data_params.train_data_path`
- `paths.val_data_template` -> `data_params.val_data_path`
- `paths.output_dir_template` -> training output directory

For `--adsorbate CH2CH3`, the defaults resolve to:

- experiment name: `oc20-2M-finetune-CH2CH3`
- training data: `data/dataset/finetune_CH2CH3_train.csv`
- validation data: `data/dataset/finetune_CH2CH3_val.csv`
- output directory: `outputs/finetune_CH2CH3`

Explicit path overrides take priority over template-generated paths:

```bash
python train_local.py \
  --config config/config.yml \
  --adsorbate CH2CH3 \
  --train-data data/dataset/custom_train.csv \
  --val-data data/dataset/custom_val.csv \
  --checkpoint checkpoint-GPT-2M/checkpoint-138890 \
  --output-dir outputs/custom_run
```

If the resolved training or validation CSV does not exist, startup stops before
training begins and reports the missing path.

## Generation

After finetuning, generation can use the same adsorbate naming convention:

```bash
python script/generate.py --config config/config.yml --adsorbate CH3 --device cpu
```

By default, the script looks for the latest `checkpoint-*` directory under:

```text
outputs/finetune_{adsorbate}
```

For example, `--adsorbate CH3` resolves a checkpoint such as:

```text
outputs/finetune_CH3/checkpoint-1162
```

Generated token IDs are saved under:

```text
outputs/finetune_{adsorbate}/generated/generated_{adsorbate}.pkl
```

The script supports both full model checkpoints and PEFT/LoRA adapter
checkpoints. For adapter checkpoints, it loads the base checkpoint from
`model_params.checkpoint_path`, then attaches the adapter checkpoint.

Useful CPU smoke test:

```bash
python script/generate.py \
  --config config/config.yml \
  --adsorbate CH3 \
  --device cpu \
  --n-generation 5 \
  --batch-size 1 \
  --max-length 128
```

Explicit generation path overrides are still available:

```bash
python script/generate.py \
  --config config/config.yml \
  --adsorbate CH3 \
  --ckpt-path outputs/finetune_CH3/checkpoint-1162 \
  --base-checkpoint checkpoint-GPT-2M/checkpoint-138890 \
  --save-path outputs/finetune_CH3/generated \
  --name smoke_CH3 \
  --device cpu
```

## Generated Pkl Analysis

Decode and validate a generated pkl file with the same adsorbate convention:

```bash
python script/ASE_check_manual.py --config config/config.yml --adsorbate CH3
```

By default, the script reads:

```text
outputs/finetune_{adsorbate}/generated/generated_{adsorbate}.pkl
```

and writes analysis artifacts to:

```text
outputs/finetune_{adsorbate}/analysis/generated_{adsorbate}
```

Outputs include:

- `xyz/sample_*.xyz`: valid structures decoded as ASE XYZ files
- `bad_samples.txt`: failed sample index, failure reason, and decoded text
- `summary.txt`: processed count and validity statistics

Useful small check:

```bash
python script/ASE_check_manual.py \
  --config config/config.yml \
  --adsorbate CH3 \
  --max-samples 20
```

Explicit pkl/output overrides are available:

```bash
python script/ASE_check_manual.py \
  --config config/config.yml \
  --adsorbate CH3 \
  --pkl-path outputs/finetune_CH3/generated/smoke_CH3.pkl \
  --output-dir outputs/finetune_CH3/analysis/smoke_CH3
```

## XYZ Metadata Index

Build a searchable index from decoded XYZ files:

```bash
python script/index_xyz_metadata.py --config config/config.yml --adsorbate CH3
```

By default, the script reads:

```text
outputs/finetune_{adsorbate}/analysis/generated_{adsorbate}/xyz
```

and writes:

```text
outputs/finetune_{adsorbate}/analysis/generated_{adsorbate}/xyz_metadata.csv
outputs/finetune_{adsorbate}/analysis/generated_{adsorbate}/xyz_invalid_adsorbate.csv
outputs/finetune_{adsorbate}/analysis/generated_{adsorbate}/material_summary.csv
```

The adsorbate check uses the last N atoms in each XYZ, where N comes from the
adsorbate formula. For `CH3`, the last four atoms must contain one C and three H
atoms; their order does not matter.

Catalyst material names are built from unique non-adsorbate elements sorted
alphabetically, so `Au-Pd` and `Pd-Au` are indexed as the same material:

```text
Au-Pd
```

Useful small check:

```bash
python script/index_xyz_metadata.py \
  --config config/config.yml \
  --adsorbate CH3 \
  --max-files 20
```

## MLP Scoring

OC20 MLP checkpoints are configured under:

```yaml
mlp:
  model_name: "EquiformerV2-31M-S2EF-OC20-All+MD"
  model_cache: "MLP_check/OC20_MLP/MLP_model"
```

Prepare or validate the local fairchem checkpoint cache:

```bash
python MLP_check/OC20_MLP/get_checkpoint.py --config config/config.yml
```

Score generated XYZ structures:

```bash
python MLP_check/OC20_MLP/mlp_scores.py --config config/config.yml --adsorbate CH3 --device cpu
```

By default, the scoring script reads:

```text
outputs/finetune_{adsorbate}/analysis/generated_{adsorbate}/xyz
```

and writes:

```text
outputs/finetune_{adsorbate}/analysis/generated_{adsorbate}/mlp_scores.csv
outputs/finetune_{adsorbate}/analysis/generated_{adsorbate}/mlp_scores_ranked.csv
```

If `xyz_metadata.csv` exists, material metadata such as `Au-Pd` and
`adsorbate_valid` is joined into the MLP score CSV.
The ranked CSV is sorted by `E_pred` from low to high.

Useful small check:

```bash
python MLP_check/OC20_MLP/mlp_scores.py \
  --config config/config.yml \
  --adsorbate CH3 \
  --device cpu \
  --max-files 5
```

You can override the MLP model location:

```bash
python MLP_check/OC20_MLP/mlp_scores.py \
  --config config/config.yml \
  --adsorbate CH3 \
  --model-cache MLP_check/OC20_MLP/MLP_model \
  --model-name EquiformerV2-31M-S2EF-OC20-All+MD \
  --device cpu
```

## Workflow Runner

For the agent-ready end-to-end runner, see `README_workflow.md`.
