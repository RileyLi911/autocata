# Model Assets

AutoCata keeps source code, configs, and workflow logic in GitHub. Large model
assets should live outside normal Git history.

Recommended distribution:

- GitHub repository: code, configs, documentation, small metadata files
- Hugging Face Hub: base checkpoints, finetuned adsorbate checkpoints, OC20 MLP
  model weights
- GitHub Release: optional fallback archive for a tagged version such as
  `v0.1.0`

Agents should report setup status and workflow results in English by default.

## Expected Local Layout

The workflow expects these paths under the AutoCata project root:

```text
checkpoint-GPT-2M/
  checkpoint-138890/
data/
  finetune_model/
    checkpoint-{adsorbate}/
MLP_check/
  OC20_MLP/
    MLP_model/
      *.pt
```

`config/adsorbates.yml` maps available adsorbate names to:

```text
data/finetune_model/checkpoint-{adsorbate}
```

## Hugging Face Hub Setup

Create a model repository, for example:

```text
RileyLi911/autocata-models
```

Keep the same relative paths inside the Hugging Face repository:

```text
checkpoint-GPT-2M/
data/finetune_model/
MLP_check/OC20_MLP/MLP_model/
```

The default model asset manifest is:

```text
config/model_assets.yml
```

Update `repo_id` there if the Hugging Face repository name changes.

## Upload From Server

Install and log in:

```bash
pip install -U huggingface_hub
huggingface-cli login
```

Create the repository if needed:

```bash
huggingface-cli repo create autocata-models --type model
```

From the AutoCata project root, upload assets:

```bash
huggingface-cli upload RileyLi911/autocata-models checkpoint-GPT-2M checkpoint-GPT-2M --repo-type model
huggingface-cli upload RileyLi911/autocata-models data/finetune_model data/finetune_model --repo-type model
huggingface-cli upload RileyLi911/autocata-models MLP_check/OC20_MLP/MLP_model MLP_check/OC20_MLP/MLP_model --repo-type model
```

## User Download

After cloning the GitHub repository:

```bash
git clone https://github.com/RileyLi911/autocata.git
cd autocata
conda env create -f envs/autocata.yml
conda activate autocata
python script/download_models.py
```

Validate local assets:

```bash
python script/download_models.py --check-only
```

Override the Hugging Face repository when needed:

```bash
python script/download_models.py --repo-id owner/autocata-models
```

Preview the download plan without network access:

```bash
python script/download_models.py --dry-run
```

## GitHub Release Fallback

GitHub Releases can be used as a backup distribution channel for frozen model
bundles. From the project root, create archives such as:

```bash
mkdir -p release_assets
zip -r release_assets/checkpoint-GPT-2M.zip checkpoint-GPT-2M
zip -r release_assets/finetune_model.zip data/finetune_model
zip -r release_assets/MLP_model.zip MLP_check/OC20_MLP/MLP_model
```

Then attach them to a release, for example `v0.1.0-models`.

## Why Not Commit Models Directly?

Large binary files make Git history heavy and slow to clone. GitHub also blocks
large normal Git files. Keeping model weights on Hugging Face Hub makes GitHub
the clean code entry point while still giving users a one-command asset setup.
