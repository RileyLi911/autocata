from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def command_output(argv: list[str]) -> dict[str, object]:
    executable = shutil.which(argv[0])
    if not executable:
        return {"available": False, "argv": argv}
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    except Exception as exc:
        return {"available": True, "argv": argv, "error": str(exc)}
    return {"available": True, "argv": argv, "returncode": result.returncode,
            "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}


def probe_torch() -> dict[str, object]:
    import torch
    if not torch.cuda.is_available() or torch.cuda.device_count() < 1:
        raise RuntimeError("PyTorch reports no CUDA device")
    device = torch.device("cuda:0")
    result = torch.ones((8, 8), device=device) @ torch.ones((8, 8), device=device)
    if not bool(torch.isfinite(result).all().item()):
        raise RuntimeError("PyTorch CUDA tensor operation returned nonfinite values")
    properties = torch.cuda.get_device_properties(0)
    return {"framework": "pytorch", "version": torch.__version__, "cuda_build": torch.version.cuda,
            "cudnn_version": torch.backends.cudnn.version(), "device_count": torch.cuda.device_count(),
            "device_name": properties.name, "compute_capability": [properties.major, properties.minor],
            "device_index": 0, "tensor_smoke_test": True}


def probe_tensorflow() -> dict[str, object]:
    import tensorflow as tf
    devices = tf.config.list_physical_devices("GPU")
    if not devices:
        raise RuntimeError("TensorFlow reports no GPU device")
    with tf.device("/GPU:0"):
        result = tf.matmul(tf.ones((8, 8)), tf.ones((8, 8)))
    if not bool(tf.reduce_all(tf.math.is_finite(result)).numpy()):
        raise RuntimeError("TensorFlow GPU tensor operation returned nonfinite values")
    return {"framework": "tensorflow", "version": tf.__version__, "gpu_count": len(devices),
            "devices": [device.name for device in devices], "tensor_smoke_test": True}


def probe_jax() -> dict[str, object]:
    import jax
    import jax.numpy as jnp
    devices = jax.devices("gpu")
    if not devices:
        raise RuntimeError("JAX reports no GPU device")
    result = jnp.ones((8, 8), device=devices[0]) @ jnp.ones((8, 8), device=devices[0])
    if not bool(jnp.isfinite(result).all()):
        raise RuntimeError("JAX GPU tensor operation returned nonfinite values")
    return {"framework": "jax", "version": jax.__version__, "devices": [str(device) for device in devices],
            "tensor_smoke_test": True}


def probe(framework: str) -> dict[str, object]:
    result: dict[str, object] = {"schema_version": 4, "probed_at": datetime.now(timezone.utc).isoformat(),
                                 "python_executable": sys.executable, "python_version": sys.version,
                                 "platform": platform.platform(), "nvidia_smi": command_output(["nvidia-smi"]),
                                 "framework": framework, "passed": False}
    try:
        if framework == "pytorch":
            result["framework_evidence"] = probe_torch()
        elif framework == "tensorflow":
            result["framework_evidence"] = probe_tensorflow()
        else:
            result["framework_evidence"] = probe_jax()
        result["passed"] = bool((result["nvidia_smi"] or {}).get("available"))
        if not result["passed"]:
            result["error"] = "nvidia-smi is unavailable"
    except Exception as exc:
        result["error"] = str(exc)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="GPU runtime integrity probe")
    parser.add_argument("--framework", choices=["pytorch", "tensorflow", "jax"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = probe(args.framework)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
