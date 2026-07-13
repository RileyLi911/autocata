import argparse
import os
import shutil
from pathlib import Path

from transformers import PreTrainedTokenizerFast
from transformers import Trainer, TrainingArguments
from peft import LoraConfig, get_peft_model


from autocata_core.modules.trainer import CustomHFTrainer
from autocata_core.modules.models import get_model
from autocata_core.modules.tokenizers import T5TokenizerForStructure

from omegaconf import OmegaConf

PROJECT_ROOT = Path(__file__).resolve().parent


def parse_args():
    parser = argparse.ArgumentParser(description="Train the AutoCata structure model from a YAML config.")
    parser.add_argument(
        "--config",
        default="config/config.yml",
        help="Path to the training config YAML file.",
    )
    parser.add_argument(
        "--adsorbate",
        help="Adsorbate name. Overrides experiment.adsorbate in the config.",
    )
    parser.add_argument(
        "--train-data",
        help="Training CSV path. Overrides the adsorbate-based template path.",
    )
    parser.add_argument(
        "--val-data",
        help="Validation CSV path. Overrides the adsorbate-based template path.",
    )
    parser.add_argument(
        "--checkpoint",
        help="Checkpoint path. Overrides model_params.checkpoint_path in the config.",
    )
    parser.add_argument(
        "--output-dir",
        help="Output directory. Overrides the adsorbate-based template path.",
    )
    return parser.parse_args()


def resolve_input_path(path):
    path = Path(path)
    if path.is_absolute():
        return path
    cwd_path = Path.cwd() / path
    if cwd_path.exists():
        return cwd_path.resolve()
    return (PROJECT_ROOT / path).resolve()


def resolve_project_path(path):
    path = Path(path)
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


def format_template(template, adsorbate):
    return str(template).format(adsorbate=adsorbate)


def path_for_message(path):
    path = Path(path)
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def require_file(path, label):
    if not Path(path).is_file():
        raise FileNotFoundError(f"Missing {label}: {path_for_message(path)}")


def prepare_params(config_path, args):
    params = OmegaConf.load(config_path)
    model_params = params.model_params
    data_params = params.data_params
    experiment = params.experiment
    paths = params.paths

    adsorbate = args.adsorbate or experiment.adsorbate
    experiment.adsorbate = adsorbate
    experiment.name = format_template(experiment.name_template, adsorbate)
    model_params.name = experiment.name

    dataset_dir = resolve_project_path(paths.dataset_dir)
    train_data_path = (
        resolve_project_path(args.train_data)
        if args.train_data
        else dataset_dir / format_template(paths.train_data_template, adsorbate)
    )
    val_data_path = (
        resolve_project_path(args.val_data)
        if args.val_data
        else dataset_dir / format_template(paths.val_data_template, adsorbate)
    )
    output_dir = (
        resolve_project_path(args.output_dir)
        if args.output_dir
        else resolve_project_path(format_template(paths.output_dir_template, adsorbate))
    )

    require_file(train_data_path, "training data")
    require_file(val_data_path, "validation data")

    if model_params.use_pretrained:
        checkpoint_path = args.checkpoint or model_params.checkpoint_path
        model_params.checkpoint_path = str(resolve_project_path(checkpoint_path))

    data_params.train_data_path = str(train_data_path)
    data_params.val_data_path = str(val_data_path)
    paths.output_dir = str(output_dir)
    return params


def build_tokenizer(model_params, data_params):
    props = "prop-" if data_params.add_props else ""

    if model_params.use_pretrained:
        return PreTrainedTokenizerFast.from_pretrained(model_params.checkpoint_path)

    tokenizer_dir = resolve_project_path(
        f"data/tokenizer/{data_params.string_type}-{props}tokenizer"
    )
    if data_params.string_type == "t5":
        tokenizer_dir = resolve_project_path(f"data/tokenizer/t5-{props}tokenizer")
        return T5TokenizerForStructure.from_pretrained(tokenizer_dir)

    return PreTrainedTokenizerFast.from_pretrained(
        tokenizer_dir,
        max_len=data_params.max_len,
    )


def build_training_args(model_params, data_params, output_dir):
    return TrainingArguments(
        output_dir=str(output_dir),
        overwrite_output_dir=True,
        do_train=True,
        do_eval=True,
        eval_strategy="steps",
        eval_steps=data_params.eval_steps,
        logging_strategy="steps",
        logging_steps=10,
        per_device_train_batch_size=data_params.batch_size,
        per_device_eval_batch_size=data_params.batch_size,
        gradient_accumulation_steps=data_params.gradient_accumulation_steps,
        save_strategy="epoch",
        run_name=model_params.name,
        report_to=[],
        num_train_epochs=data_params.num_epochs,
        save_total_limit=10,
        learning_rate=data_params.learning_rate,
        warmup_steps=data_params.warmup_steps,
        dataloader_num_workers=data_params.num_workers,
        tf32=data_params.tf32,
    )


def main(args):
    os.environ["WANDB_PROJECT"] = "AutoCata"

    config_path = resolve_input_path(args.config)
    params = prepare_params(config_path, args)
    model_params = params.model_params
    data_params = params.data_params

    tokenizer = build_tokenizer(model_params, data_params)
    base_model, config, dataset, data_collator = get_model(
        model_params,
        data_params,
        tokenizer,
    )

    if model_params.use_pretrained:
        model = base_model.from_pretrained(
            pretrained_model_name_or_path=model_params.checkpoint_path
        )

        lora_config = LoraConfig(
            r=model_params.r,
            lora_alpha=model_params.lora_alpha,
            lora_dropout=model_params.lora_dropout,
            task_type=model_params.task_type,
        )

        if model_params.use_lora:
            model = get_peft_model(model, lora_config)

    else:
        model = base_model(config=config)

    output_dir = Path(params.paths.output_dir)
    training_args = build_training_args(model_params, data_params, output_dir)

    trainer_class = CustomHFTrainer if model_params.use_numerical_encoding else Trainer
    trainer_kwargs = {
        "model": model,
        "args": training_args,
        "data_collator": data_collator,
        "train_dataset": dataset["train"],
        "eval_dataset": dataset["val"],
        "tokenizer": tokenizer,
    }
    if model_params.use_numerical_encoding:
        trainer_kwargs.update(
            use_numerical_encodings=True,
            d_model=model_params.n_embd,
            vocab_size=len(tokenizer.get_vocab()),
        )

    trainer = trainer_class(**trainer_kwargs)
    trainer.train()
    trainer.save_model(str(output_dir))
    shutil.copy(config_path, output_dir / "config.yml")


if __name__ == "__main__":
    args = parse_args()
    main(args)
