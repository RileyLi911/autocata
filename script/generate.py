import argparse
import os
import pickle
import re
from contextlib import nullcontext
from pathlib import Path

import torch
from omegaconf import OmegaConf
from tqdm import tqdm
from transformers import GPT2LMHeadModel, PreTrainedTokenizerFast

try:
    from peft import PeftModel
except ImportError:
    PeftModel = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args():
    p = argparse.ArgumentParser(description="Generate structures from a finetuned CatGPT checkpoint.")
    p.add_argument("--config", default="config/config.yml", help="Path to the training config YAML file.")
    p.add_argument("--adsorbate", help="Adsorbate name. Overrides experiment.adsorbate in the config.")

    # Path overrides. Old names are kept so existing commands still work.
    p.add_argument("--ckpt-path", "--checkpoint", dest="ckpt_path", help="Finetuned checkpoint path.")
    p.add_argument("--base-checkpoint", help="Base model checkpoint for LoRA adapter checkpoints.")
    p.add_argument("--tokenizer-path", help="Tokenizer path. Defaults to checkpoint, then base checkpoint.")
    p.add_argument("--save-path", help="Directory for generated pickle files.")
    p.add_argument("--name", help="Output pkl name without .pkl.")

    # Generation scale. Defaults are intentionally small enough for CPU smoke tests.
    p.add_argument("--n-generation", type=int)

    # Sampling
    p.add_argument("--top-k", type=int)
    p.add_argument("--top-p", type=float)
    p.add_argument("--temperature", type=float)

    # Runtime
    p.add_argument("--batch-size", type=int, help="Batch size. Defaults to 8 on CPU and 256 on CUDA.")
    p.add_argument("--max-length", type=int, help="Maximum generated token length. Defaults to data_params.max_len.")
    p.add_argument("--device", default="auto", help="auto, cpu, cuda, or cuda:N.")
    p.add_argument("--use-bf16", action="store_true", help="Use CUDA bf16 autocast during generation.")

    # Prompt
    p.add_argument("--input-prompt", default="")

    # Storage
    p.add_argument(
        "--dtype",
        default=None,
        choices=["int64", "int32", "int16"],
        help="dtype for saved token ids. int32 recommended; int16 if vocab<32768.",
    )
    return p.parse_args()


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


def checkpoint_sort_key(path):
    match = re.fullmatch(r"checkpoint-(\d+)", path.name)
    if match:
        return int(match.group(1))
    return -1


def find_latest_checkpoint(output_dir):
    output_dir = Path(output_dir)
    if not output_dir.is_dir():
        raise FileNotFoundError(f"Missing finetune output directory: {path_for_message(output_dir)}")

    checkpoints = [
        path for path in output_dir.iterdir()
        if path.is_dir() and path.name.startswith("checkpoint")
    ]
    if not checkpoints:
        raise FileNotFoundError(f"No checkpoint-* directory found in: {path_for_message(output_dir)}")
    return max(checkpoints, key=lambda path: (checkpoint_sort_key(path), path.stat().st_mtime))


def has_tokenizer(path):
    path = Path(path)
    return (path / "tokenizer.json").is_file() or (path / "vocab.txt").is_file()


def is_adapter_checkpoint(path):
    return (Path(path) / "adapter_config.json").is_file()


def load_params(config_path, args):
    params = OmegaConf.load(config_path)
    adsorbate = args.adsorbate or params.experiment.adsorbate
    params.experiment.adsorbate = adsorbate
    params.experiment.name = format_template(params.experiment.name_template, adsorbate)
    params.model_params.name = params.experiment.name

    output_dir = resolve_project_path(format_template(params.paths.output_dir_template, adsorbate))
    ckpt_path = resolve_project_path(args.ckpt_path) if args.ckpt_path else find_latest_checkpoint(output_dir)
    base_checkpoint = resolve_project_path(args.base_checkpoint or params.model_params.checkpoint_path)

    if args.tokenizer_path:
        tokenizer_path = resolve_project_path(args.tokenizer_path)
    elif has_tokenizer(ckpt_path):
        tokenizer_path = ckpt_path
    elif has_tokenizer(base_checkpoint):
        tokenizer_path = base_checkpoint
    else:
        tokenizer_path = resolve_project_path(f"data/tokenizer/{params.data_params.string_type}-tokenizer")

    generation_dir_name = OmegaConf.select(params, "paths.generation_dir_name", default="generated")
    save_path = (
        resolve_project_path(args.save_path)
        if args.save_path
        else output_dir / generation_dir_name
    )
    name = args.name or f"generated_{adsorbate}"
    max_length = args.max_length or params.data_params.max_len

    return params, ckpt_path, base_checkpoint, tokenizer_path, save_path, name, max_length


def select_device(device_arg):
    if device_arg == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if device_arg.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is not available. Use --device cpu for CPU generation.")
    return torch.device(device_arg)


def load_model(ckpt_path, base_checkpoint, device):
    ckpt_path = Path(ckpt_path)
    base_checkpoint = Path(base_checkpoint)

    if is_adapter_checkpoint(ckpt_path):
        if PeftModel is None:
            raise ImportError("This checkpoint is a PEFT/LoRA adapter, but peft is not installed.")
        if not base_checkpoint.is_dir():
            raise FileNotFoundError(f"Missing base checkpoint: {path_for_message(base_checkpoint)}")
        base_model = GPT2LMHeadModel.from_pretrained(str(base_checkpoint))
        model = PeftModel.from_pretrained(base_model, str(ckpt_path))
    else:
        model = GPT2LMHeadModel.from_pretrained(str(ckpt_path))

    model.to(device)
    model.eval()
    return model


def autocast_context(device, use_bf16):
    if device.type == "cuda" and use_bf16:
        return torch.autocast(device_type="cuda", dtype=torch.bfloat16)
    return nullcontext()


def main():
    args = parse_args()
    config_path = resolve_project_path(args.config)
    params, ckpt_path, base_checkpoint, tokenizer_path, save_path, name, max_length = load_params(
        config_path,
        args,
    )
    device = select_device(args.device)
    n_generation = (
        args.n_generation
        if args.n_generation is not None
        else OmegaConf.select(params, "generation.n_generation", default=100)
    )
    top_k = args.top_k if args.top_k is not None else OmegaConf.select(params, "generation.top_k", default=30)
    top_p = args.top_p if args.top_p is not None else OmegaConf.select(params, "generation.top_p", default=0.9)
    temperature = (
        args.temperature
        if args.temperature is not None
        else OmegaConf.select(params, "generation.temperature", default=1.0)
    )
    save_dtype_name = args.dtype if args.dtype is not None else OmegaConf.select(params, "generation.dtype", default="int32")
    if save_dtype_name not in ["int64", "int32", "int16"]:
        raise ValueError("generation.dtype must be one of: int64, int32, int16")
    default_batch_size = OmegaConf.select(
        params,
        "generation.batch_size_cuda" if device.type == "cuda" else "generation.batch_size_cpu",
        default=256 if device.type == "cuda" else 8,
    )
    batch_size = args.batch_size if args.batch_size is not None else default_batch_size

    if device.type == "cuda":
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True

    print(f"[config] adsorbate={params.experiment.adsorbate}")
    print(f"[paths] checkpoint={path_for_message(ckpt_path)}")
    if is_adapter_checkpoint(ckpt_path):
        print(f"[paths] base_checkpoint={path_for_message(base_checkpoint)}")
    print(f"[paths] tokenizer={path_for_message(tokenizer_path)}")
    print(f"[paths] save_path={path_for_message(save_path)}")
    print(f"[runtime] device={device} batch_size={batch_size} n_generation={n_generation}")

    tokenizer = PreTrainedTokenizerFast.from_pretrained(str(tokenizer_path))

    if tokenizer.pad_token_id is None:
        if tokenizer.eos_token_id is not None:
            tokenizer.pad_token = tokenizer.eos_token
        else:
            tokenizer.add_special_tokens({"pad_token": "<pad>"})

    model = load_model(ckpt_path, base_checkpoint, device)

    prompt_ids = tokenizer.encode(args.input_prompt, add_special_tokens=True)
    if len(prompt_ids) == 0:
        if tokenizer.bos_token_id is not None:
            prompt_ids = [tokenizer.bos_token_id]
        elif tokenizer.eos_token_id is not None:
            prompt_ids = [tokenizer.eos_token_id]
        else:
            prompt_ids = [tokenizer.pad_token_id]

    prompt = torch.tensor(prompt_ids, dtype=torch.long, device=device).unsqueeze(0)
    eos_id = tokenizer.eos_token_id
    save_dtype = {"int64": torch.int64, "int32": torch.int32, "int16": torch.int16}[save_dtype_name]

    generated = []
    total = n_generation
    total_len = 0
    hit_eos = 0
    saved_n = 0
    max_len_seen = 0

    with tqdm(total=total) as pbar:
        n_done = 0
        while n_done < total:
            cur_bs = min(batch_size, total - n_done)
            input_ids = prompt.repeat(cur_bs, 1)

            with torch.inference_mode():
                with autocast_context(device, args.use_bf16):
                    out = model.generate(
                        input_ids=input_ids,
                        max_length=max_length,
                        do_sample=True,
                        top_k=top_k,
                        top_p=top_p,
                        temperature=temperature,
                        pad_token_id=tokenizer.pad_token_id,
                        eos_token_id=tokenizer.eos_token_id,
                        use_cache=True,
                    )

            out_cpu = out.detach().to("cpu")

            for j in range(out_cpu.size(0)):
                seq1d = out_cpu[j]

                found_eos = False
                if eos_id is not None:
                    eos_pos = (seq1d == eos_id).nonzero(as_tuple=False)
                    if eos_pos.numel() > 0:
                        k = int(eos_pos[0].item())
                        seq1d = seq1d[:k + 1]
                        found_eos = True

                seq1d = seq1d.contiguous().clone().to(save_dtype)
                generated.append(seq1d.unsqueeze(0))

                length = int(seq1d.numel())
                total_len += length
                saved_n += 1
                hit_eos += int(found_eos)
                max_len_seen = max(max_len_seen, length)

            n_done += cur_bs
            pbar.update(cur_bs)

    os.makedirs(save_path, exist_ok=True)
    out_pkl = save_path / f"{name}.pkl"

    with open(out_pkl, "wb") as fw:
        pickle.dump(generated, fw, protocol=pickle.HIGHEST_PROTOCOL)

    print(f"[done] wrote: {path_for_message(out_pkl)}")
    if saved_n > 0:
        print(f"[stats] n={saved_n} avg_len={total_len / saved_n:.1f} hit_eos={hit_eos / saved_n:.3f} max_len={max_len_seen}")
        print(f"[stats] saved dtype={save_dtype_name}")

    try:
        size_mb = os.path.getsize(out_pkl) / 1024**2
        print(f"[file] size={size_mb:.2f} MB")
    except OSError:
        pass


if __name__ == "__main__":
    main()
