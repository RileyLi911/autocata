"""Lazy ASE calculator adapters; importing this module never loads a model."""
from importlib import import_module, metadata
from pathlib import Path


DEFAULT_MODELS = {"mace": "medium-mpa-0", "chgnet": "0.3.0"}
PACKAGES = {"mace": "mace-torch", "chgnet": "chgnet"}


def package_versions(backend):
    result = {}
    for name in (PACKAGES[backend], "torch", "ase", "numpy"):
        try:
            result[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            result[name] = "not installed"
    return result


def build_calculator(backend, model, checkpoint=None, device="cpu", dtype="float64"):
    if backend not in DEFAULT_MODELS:
        raise ValueError(f"Unsupported backend: {backend}")
    if device not in ("cpu", "cuda"):
        raise ValueError("device must be cpu or cuda")
    if checkpoint and not Path(checkpoint).is_file():
        raise FileNotFoundError(f"Checkpoint does not exist: {checkpoint}")
    try:
        torch = import_module("torch")
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA was requested but is unavailable; select cpu explicitly.")
        if backend == "mace":
            calculators = import_module("mace.calculators")
            if checkpoint:
                return calculators.MACECalculator(
                    model_paths=str(checkpoint), device=device, default_dtype=dtype,
                )
            return calculators.mace_mp(
                model=model, device=device, default_dtype=dtype, dispersion=False,
            )
        model_cls = import_module("chgnet.model.model").CHGNet
        calculator_cls = import_module("chgnet.model.dynamics").CHGNetCalculator
        network = (model_cls.from_file(str(checkpoint)) if checkpoint else
                   model_cls.load(model_name=model, use_device=device))
        # ASE's CHGNet calculator already converts eV/atom into total eV.
        return calculator_cls(model=network, use_device=device)
    except ImportError as exc:
        raise RuntimeError(
            f"Cannot import {backend} dependencies. Install {PACKAGES[backend]} "
            "in its separate environment; see MLP_check/README.md. "
            f"Original error: {exc}"
        ) from exc
