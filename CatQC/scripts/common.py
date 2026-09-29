from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import tempfile
import shlex
from pathlib import Path
from statistics import median
from typing import Any, Iterable


SUCCESS_STATUSES = {"success", "ok", "done", "completed", "complete", "valid"}
FAILED_STATUSES = {"error", "failed", "failure", "invalid"}
UNCONVERGED_STATUSES = {"unconverged", "not_converged", "nonconverged"}
MISSING_STATUSES = {"missing", "absent"}
SKIPPED_STATUSES = {"skipped", "not_requested", "blocked", "gated"}

SUPPORTED_STRUCTURE_SUFFIXES = {".xyz": "xyz", ".extxyz": "extxyz", ".cif": "cif"}
POSCAR_FILENAMES = {"poscar", "contcar"}


def structure_file_type(path: Path) -> str | None:
    """Return the supported structure type, including extensionless POSCAR files."""
    lowered_name = path.name.casefold()
    if lowered_name in POSCAR_FILENAMES or path.suffix.casefold() in {".poscar", ".contcar", ".vasp"}:
        return "poscar"
    return SUPPORTED_STRUCTURE_SUFFIXES.get(path.suffix.casefold())


def _structure_source_files(path: Path) -> tuple[list[Path], str]:
    path = path.resolve()
    if path.is_file():
        file_type = structure_file_type(path)
        if file_type is None:
            raise ValueError(f"unsupported structure input format: {path}")
        return [path], "single_file"
    if not path.is_dir():
        raise FileNotFoundError(f"structure input does not exist: {path}")
    candidates = [item for item in path.iterdir() if item.is_file() and not item.name.startswith(".")]
    supported = [(item, structure_file_type(item)) for item in candidates]
    supported = [(item, kind) for item, kind in supported if kind is not None]
    if not supported:
        raise ValueError(f"structure input directory contains no supported files: {path}")
    unsupported = [item for item in candidates if structure_file_type(item) is None]
    if unsupported:
        names = ", ".join(item.name for item in sorted(unsupported, key=lambda p: p.name.casefold()))
        raise ValueError(f"unsupported files in structure input directory: {names}")
    kinds = {kind for _, kind in supported}
    if len(kinds) > 1:
        rendered = ", ".join(sorted(kinds))
        raise ValueError(f"mixed structure formats in one input directory are not allowed: {rendered}")
    return [item for item, _ in sorted(supported, key=lambda pair: pair[0].name.casefold())], "directory_sorted_filename"


def read_structure_frames(path: Path) -> tuple[list[Any], dict[str, Any]]:
    """Read a single structure file or a deterministically ordered structure directory.

    ASE remains the only coordinate parser. The returned provenance is suitable for
    freezing in benchmark_config/input-inspection artifacts and records every source
    file hash without modifying the user's inputs.
    """
    files, ordering_rule = _structure_source_files(path)
    try:
        from ase.io import read
    except ImportError as exc:
        raise RuntimeError("ASE is required to read structure inputs") from exc
    frames: list[Any] = []
    sources: list[dict[str, Any]] = []
    for source in files:
        loaded = read(str(source), index=":")
        source_frames = loaded if isinstance(loaded, list) else [loaded]
        if not source_frames:
            raise ValueError(f"structure input contains no frames: {source}")
        frames.extend(frame.copy() for frame in source_frames)
        sources.append({
            "path": str(source),
            "file_type": structure_file_type(source),
            "sha256": sha256_file(source),
            "frame_count": len(source_frames),
        })
    if not frames:
        raise ValueError(f"structure input contains no frames: {path}")
    kinds = sorted({str(item["file_type"]) for item in sources})
    return frames, {
        "input_path": str(Path(path).resolve()),
        "input_mode": ordering_rule,
        "file_type": kinds[0] if len(kinds) == 1 else "mixed",
        "ordering_rule": ordering_rule,
        "source_files": sources,
        "frame_count": len(frames),
    }


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def frame_relative_dir(model_key: str, task: str, structure_index: int) -> str:
    """Return the canonical, collision-resistant directory for one frame."""
    if task not in {"single_point", "relaxation"}:
        raise ValueError(f"unsupported task: {task}")
    if int(structure_index) < 0:
        raise ValueError("structure_index must be nonnegative")
    return f"models/{model_key}/frames/{task}/{int(structure_index):08d}"


def frame_paths(root: Path, model_key: str, task: str, structure_index: int) -> dict[str, Path]:
    base = root / frame_relative_dir(model_key, task, structure_index)
    paths = {
        "base": base,
        "input": base / "input",
        "work": base / "work",
        "outputs": base / "outputs",
        "errors": base / "errors",
        "input_structure": base / "input" / "input_structure.extxyz",
        "input_manifest": base / "input" / "input_manifest.json",
        "result": base / "outputs" / "result.json",
        "relaxed_structure": base / "outputs" / "relaxed_structure.extxyz",
        "trajectory": base / "outputs" / "trajectory.extxyz",
        "error": base / "errors" / "error.json",
        "traceback": base / "errors" / "traceback.txt",
        "cleanup": base / "cleanup.json",
        "manifest": base / "frame_manifest.json",
    }
    return paths


def file_evidence(path: Path, root: Path) -> dict[str, Any]:
    relative = str(path.relative_to(root)).replace("\\", "/")
    if not path.is_file():
        raise FileNotFoundError(path)
    return {"path": relative, "size_bytes": path.stat().st_size, "sha256": sha256_file(path)}


def validate_frame_artifact(
    root: Path,
    model_key: str,
    task: str,
    structure_index: int,
    *,
    expected_run_id: str | None = None,
    expected_frame_count: int | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Validate one frame directory without importing a model or calculator."""
    paths = frame_paths(root, model_key, task, structure_index)
    errors: list[str] = []
    for key, label in (("input_structure", "input/input_structure.extxyz"), ("input_manifest", "input/input_manifest.json")):
        if not paths[key].is_file():
            errors.append(f"missing {label}")
    manifest: dict[str, Any] | None = None
    if not paths["manifest"].is_file():
        errors.append("missing frame_manifest.json")
    else:
        try:
            manifest = load_json(paths["manifest"])
        except Exception as exc:
            errors.append(f"invalid frame_manifest.json: {exc}")
    if manifest is not None:
        if manifest.get("schema_version") != 4:
            errors.append("frame manifest schema_version must be 4")
        if expected_run_id is not None and manifest.get("run_id") != expected_run_id:
            errors.append("frame manifest run_id mismatch")
        if manifest.get("model_key") != model_key or manifest.get("task") != task:
            errors.append("frame manifest model/task mismatch")
        if manifest.get("structure_index") != int(structure_index):
            errors.append("frame manifest structure_index mismatch")
        if expected_frame_count is not None and not 0 <= int(structure_index) < expected_frame_count:
            errors.append("structure_index is outside expected frame range")
        if not isinstance(manifest.get("input_structure_sha256"), str) or len(manifest["input_structure_sha256"]) != 64:
            errors.append("frame manifest requires input_structure_sha256")
        lifecycle = manifest.get("lifecycle") or {}
        if lifecycle.get("calculator_created") is not True or lifecycle.get("calculator_fresh") is not True:
            errors.append("frame manifest does not prove a fresh calculator")
        if lifecycle.get("calculator_released") is not True or lifecycle.get("cleanup_completed") is not True:
            errors.append("frame manifest does not prove calculator cleanup")
        if manifest.get("status") not in {"completed", "failed", "unconverged"}:
            errors.append("frame manifest status is not terminal")
        if paths["input_structure"].is_file() and sha256_file(paths["input_structure"]) != manifest.get("input_structure_sha256"):
            errors.append("input_structure.extxyz hash does not match frame manifest")
        outputs = manifest.get("outputs") or {}
        for name, evidence in outputs.items():
            if not isinstance(evidence, dict) or not evidence.get("path"):
                errors.append(f"output evidence {name} is malformed")
                continue
            output_path = root / str(evidence["path"])
            if not output_path.is_file():
                errors.append(f"output evidence {name} points to a missing file")
            elif evidence.get("sha256") != sha256_file(output_path):
                errors.append(f"output evidence {name} hash mismatch")
    if not paths["cleanup"].is_file():
        errors.append("missing cleanup.json")
    else:
        try:
            cleanup = load_json(paths["cleanup"])
            if cleanup.get("calculator_released") is not True or cleanup.get("cleanup_completed") is not True:
                errors.append("cleanup.json does not prove calculator release")
        except Exception as exc:
            errors.append(f"invalid cleanup.json: {exc}")
    status = (manifest or {}).get("status")
    if status == "completed":
        if (root / "models_manifest.json").is_file():
            from catalysis_selection import frozen_model, observation_errors
            try:
                model = frozen_model(root / "models" / model_key)
                errors.extend(observation_errors(manifest or {}, model, root))
            except (ValueError, OSError, KeyError) as exc:
                errors.append(str(exc))
        if not paths["result"].is_file():
            errors.append("completed frame is missing outputs/result.json")
        elif manifest is not None and "result" not in (manifest.get("outputs") or {}):
            errors.append("completed frame manifest lacks result output evidence")
        if task == "relaxation" and not paths["relaxed_structure"].is_file():
            errors.append("completed Relax frame is missing relaxed_structure.extxyz")
        elif task == "relaxation" and manifest is not None and "relaxed_structure" not in (manifest.get("outputs") or {}):
            errors.append("Relax frame manifest lacks relaxed_structure output evidence")
        if paths["error"].is_file():
            errors.append("completed frame must not contain errors/error.json")
    elif status in {"failed", "unconverged"}:
        if not paths["error"].is_file():
            errors.append("failed frame is missing errors/error.json")
        elif manifest is not None:
            error_evidence = manifest.get("error") or {}
            if not isinstance(error_evidence, dict) or not error_evidence.get("path"):
                errors.append("failed frame manifest lacks error evidence")
            else:
                error_path = root / str(error_evidence["path"])
                if not error_path.is_file():
                    errors.append("failed frame error evidence points to a missing file")
                elif error_evidence.get("sha256") != sha256_file(error_path):
                    errors.append("failed frame error evidence hash mismatch")
        if paths["result"].is_file() and strict:
            errors.append("failed frame must not be represented as a successful result")
    return {"valid": not errors, "errors": errors, "manifest": manifest, "paths": paths}


def canonical_json_sha256(data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_extxyz_header(header: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for token in shlex.split(header):
        if "=" in token:
            key, value = token.split("=", 1)
            values[key] = value
    return values


def read_extxyz_structure(path: Path) -> dict[str, Any]:
    """Read one extended-XYZ structure for deterministic POSCAR export."""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if len(lines) < 2:
        raise ValueError(f"truncated extxyz: {path}")
    try:
        count = int(lines[0].strip())
    except ValueError as exc:
        raise ValueError(f"invalid extxyz atom count: {path}") from exc
    if count < 1 or len(lines) < count + 2:
        raise ValueError(f"extxyz atom block is incomplete: {path}")
    header = _parse_extxyz_header(lines[1])
    lattice_raw = header.get("Lattice") or header.get("lattice")
    if not lattice_raw:
        raise ValueError("Relaxed extxyz lacks Lattice; cannot create a valid POSCAR")
    try:
        lattice_values = [float(value) for value in shlex.split(lattice_raw)]
    except ValueError as exc:
        raise ValueError("extxyz Lattice contains nonnumeric values") from exc
    if len(lattice_values) != 9:
        raise ValueError("extxyz Lattice must contain 9 values")
    properties = header.get("Properties", "")
    property_parts = [part for part in properties.split(":") if part]
    if len(property_parts) < 3 or property_parts[0] != "species":
        raise ValueError("extxyz Properties must start with species")
    specs: list[tuple[str, int]] = []
    offset = 0
    cursor = 0
    while cursor + 2 < len(property_parts):
        name, _kind, width_text = property_parts[cursor:cursor + 3]
        try:
            width = int(width_text)
        except ValueError as exc:
            raise ValueError(f"invalid extxyz property width for {name}") from exc
        specs.append((name, offset))
        offset += width
        cursor += 3
    property_width = offset
    position_offset = dict(specs).get("pos")
    if position_offset is None:
        raise ValueError("extxyz Properties must contain pos:R:3")
    mask_offset = dict(specs).get("move_mask")
    fixed_offset = dict(specs).get("fixed")
    selective_offset = dict(specs).get("selective_dynamics")
    positions: list[tuple[float, float, float]] = []
    species: list[str] = []
    movable: list[tuple[bool, bool, bool]] = []
    for line in lines[2:2 + count]:
        fields = line.split()
        if len(fields) < property_width:
            raise ValueError("extxyz atom row is incomplete")
        species.append(fields[dict(specs)["species"]])
        positions.append(tuple(float(value) for value in fields[position_offset:position_offset + 3]))
        if mask_offset is not None:
            values = fields[mask_offset:mask_offset + 3]
            movable.append(tuple(str(value).strip().lower() in {"t", "true", "1"} for value in values))
        elif selective_offset is not None:
            values = fields[selective_offset:selective_offset + 3]
            movable.append(tuple(str(value).strip().lower() in {"t", "true", "1"} for value in values))
        elif fixed_offset is not None:
            values = fields[fixed_offset:fixed_offset + 3]
            movable.append(tuple(str(value).strip().lower() not in {"t", "true", "1"} for value in values))
        else:
            movable.append((True, True, True))
    return {
        "comment": lines[1],
        "count": count,
        "species": species,
        "positions": positions,
        "lattice": [lattice_values[0:3], lattice_values[3:6], lattice_values[6:9]],
        "pbc": header.get("pbc") or header.get("PBC") or "T T T",
        "movable": movable,
    }


def write_poscar(structure: dict[str, Any], path: Path, comment: str) -> None:
    species = structure["species"]
    positions = structure["positions"]
    lattice = structure["lattice"]
    ordered_elements: list[str] = []
    for element in species:
        if element not in ordered_elements:
            ordered_elements.append(element)
    lines = [f"{comment} pbc={structure.get('pbc', 'T T T')}", "1.0"]
    lines.extend("  ".join(f"{float(value):.16g}" for value in vector) for vector in lattice)
    lines.append("  ".join(ordered_elements))
    lines.append("  ".join(str(species.count(element)) for element in ordered_elements))
    lines.append("Selective dynamics")
    lines.append("Cartesian")
    movable = structure.get("movable") or [(True, True, True)] * len(species)
    for element in ordered_elements:
        for index, current in enumerate(species):
            if current != element:
                continue
            flags = " ".join("T" if flag else "F" for flag in movable[index])
            lines.append("  ".join(f"{float(value):.16g}" for value in positions[index]) + "  " + flags)
    atomic_write_text(path, "\n".join(lines))


def validate_poscar(path: Path) -> dict[str, Any]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if len(lines) < 8:
        raise ValueError(f"POSCAR is truncated: {path}")
    scale = float(lines[1])
    if scale <= 0:
        raise ValueError(f"POSCAR scale must be positive: {path}")
    lattice = [[float(value) for value in lines[index].split()] for index in range(2, 5)]
    if any(len(vector) != 3 for vector in lattice):
        raise ValueError(f"POSCAR lattice is invalid: {path}")
    elements = lines[5].split()
    counts = [int(value) for value in lines[6].split()]
    if not elements or len(elements) != len(counts) or any(value < 1 for value in counts):
        raise ValueError(f"POSCAR element/count lines are invalid: {path}")
    offset = 7
    if lines[offset].lower().startswith(("selective",)):
        offset += 1
    if lines[offset].lower().startswith(("direct", "cartesian")):
        offset += 1
    total = sum(counts)
    if len(lines) < offset + total:
        raise ValueError(f"POSCAR coordinate block is incomplete: {path}")
    return {"elements": elements, "counts": counts, "atom_count": total, "lattice": lattice}


def normalize_status(value: Any) -> str:
    status = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if status in SUCCESS_STATUSES:
        return "success"
    if status in FAILED_STATUSES:
        return "failed"
    if status in UNCONVERGED_STATUSES:
        return "unconverged"
    if status in MISSING_STATUSES:
        return "missing"
    if status in SKIPPED_STATUSES:
        return "skipped"
    return status or "unknown"


def as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number


def assess_result_row(
    row: dict[str, Any],
    task: str,
    result_validation: dict[str, Any],
    relaxation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one authoritative numerical-validity decision for a result row."""
    source_status = normalize_status(row.get("status"))
    relaxation = relaxation or {}
    unconverged_policy = relaxation.get("unconverged_policy", "record_and_exclude")
    status_allows_numeric = source_status == "success" or (
        task == "Relax"
        and source_status == "unconverged"
        and unconverged_policy == "record_as_numeric_valid"
    )
    reasons: list[str] = []
    protocol_reasons: list[str] = []
    energy = as_float(row.get("E_adsorption_eV"))

    if status_allows_numeric:
        if energy is None:
            reasons.append("E_adsorption_eV is missing, nonnumeric, NaN, or infinite")

        component_names = (
            "E_slab_plus_adsorbate_eV", "E_slab_eV", "E_adsorbate_eV"
        )
        components = {name: as_float(row.get(name)) for name in component_names}
        if result_validation.get("require_energy_components", True):
            missing = [name for name, value in components.items() if value is None]
            if missing:
                reasons.append("missing or nonfinite energy components: " + ", ".join(missing))
        if energy is not None and all(value is not None for value in components.values()):
            reconstructed = (
                components["E_slab_plus_adsorbate_eV"]
                - components["E_slab_eV"]
                - components["E_adsorbate_eV"]
            )
            tolerance = float(result_validation.get("energy_identity_tolerance_eV", 1e-6))
            if abs(energy - reconstructed) > tolerance:
                reasons.append(
                    "energy identity mismatch: "
                    f"reported={energy:.12g}, reconstructed={reconstructed:.12g}, "
                    f"tolerance={tolerance:.12g} eV"
                )

        limit = result_validation.get("max_abs_adsorption_energy_eV")
        if limit is not None and energy is not None and abs(energy) > float(limit):
            reasons.append(
                f"abs(E_adsorption_eV)={abs(energy):.12g} exceeds configured limit {float(limit):.12g} eV"
            )

        if task == "Relax":
            converged = as_bool(row.get("converged"))
            if converged is None:
                reasons.append("converged must be an explicit boolean for Relax")
            elif source_status == "success" and not converged:
                reasons.append("status=success conflicts with converged=false")
            elif source_status == "unconverged" and converged:
                reasons.append("status=unconverged conflicts with converged=true")

            if source_status == "success" and result_validation.get("require_relaxation_fmax", True):
                measured_fmax = as_float(row.get("fmax_eV_per_A"))
                threshold = as_float(relaxation.get("fmax_eV_per_A"))
                if measured_fmax is None:
                    reasons.append("successful Relax row lacks finite fmax_eV_per_A")
                elif threshold is None:
                    reasons.append("relaxation protocol lacks finite fmax_eV_per_A")
                elif measured_fmax > threshold + float(result_validation.get("force_tolerance_eV_per_A", 1e-8)):
                    reasons.append(
                        f"fmax_eV_per_A={measured_fmax:.12g} exceeds convergence threshold {threshold:.12g}"
                    )

    if task == "Relax" and source_status in {"success", "unconverged"}:
        optimizer = str(row.get("optimizer") or "").strip().upper()
        primary = str(relaxation.get("optimizer") or "FIRE").strip().upper()
        fallback = str(relaxation.get("fallback_optimizer") or "BFGS").strip().upper()
        if optimizer not in {primary, fallback}:
            protocol_reasons.append(f"optimizer must be {primary} or approved fallback {fallback}")
        if optimizer == fallback:
            fallback_reason = str(row.get("optimizer_fallback_reason") or "").strip()
            exception_ids = str(row.get("scientific_exception_ids") or "").strip()
            if not fallback_reason:
                protocol_reasons.append(f"{fallback} requires optimizer_fallback_reason")
            if not exception_ids:
                protocol_reasons.append(f"{fallback} requires a scientific_exception_id")

        baseline_steps = int(relaxation.get("max_steps", 1000))
        rerun_steps = int(relaxation.get("unconverged_rerun_max_steps", 5000))
        row_max_steps = as_int(row.get("max_steps"))
        attempt_count = as_int(row.get("attempt_count"))
        n_steps = as_int(row.get("n_steps"))
        if row_max_steps not in {baseline_steps, rerun_steps}:
            protocol_reasons.append(
                f"max_steps must be baseline {baseline_steps} or required rerun {rerun_steps}"
            )
        if row_max_steps == rerun_steps and (attempt_count is None or attempt_count < 2):
            protocol_reasons.append(f"{rerun_steps}-step result requires attempt_count >= 2")
        if source_status == "unconverged" and row_max_steps != rerun_steps:
            protocol_reasons.append(
                f"unconverged {baseline_steps}-step result must be rerun with max_steps={rerun_steps}"
            )
        if n_steps is None or n_steps < 0:
            protocol_reasons.append("Relax row requires nonnegative integer n_steps")
        elif row_max_steps is not None and n_steps > row_max_steps:
            protocol_reasons.append("n_steps exceeds recorded max_steps")

        if as_bool(row.get("cell_fixed")) is not True:
            protocol_reasons.append("Relax must keep the cell fixed")
        fixed_indices_raw = row.get("fixed_atom_indices")
        try:
            fixed_indices = json.loads(fixed_indices_raw) if isinstance(fixed_indices_raw, str) else fixed_indices_raw
        except (TypeError, ValueError, json.JSONDecodeError):
            fixed_indices = None
        fixed_max = as_float(row.get("fixed_atom_max_displacement_A"))
        fixed_tolerance = as_float(row.get("fixed_atom_displacement_tolerance_A"))
        fixed_violation = as_bool(row.get("fixed_atom_displacement_violation"))
        if not isinstance(fixed_indices, list) or not fixed_indices:
            protocol_reasons.append("Relax lacks fixed_atom_indices evidence")
        if fixed_max is None or fixed_tolerance is None:
            protocol_reasons.append("Relax lacks fixed-atom displacement evidence")
        if fixed_violation is not False:
            protocol_reasons.append("fixed bottom-layer atom displacement exceeds the configured tolerance")
        elif fixed_max is not None and fixed_tolerance is not None and fixed_max > fixed_tolerance:
            protocol_reasons.append("fixed bottom-layer atom displacement exceeds the configured tolerance")
        slab_layers = as_int(row.get("slab_layer_count"))
        fixed_layers = as_int(row.get("fixed_layer_count"))
        if slab_layers is None or slab_layers < 1:
            protocol_reasons.append("slab_layer_count must be a positive integer")
        else:
            expected_fixed = 1 if slab_layers <= 5 else 2
            if fixed_layers != expected_fixed:
                protocol_reasons.append(
                    f"slab with {slab_layers} layers must fix bottom {expected_fixed} layer(s)"
                )

    reasons.extend(protocol_reasons)
    valid = status_allows_numeric and not reasons
    effective_status = source_status
    if status_allows_numeric and reasons:
        effective_status = "failed"
    return {
        "valid": valid,
        "source_status": source_status,
        "effective_status": effective_status,
        "energy": energy,
        "reasons": reasons,
        "protocol_reasons": protocol_reasons,
    }


def rankdata(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        average_rank = (start + 1 + end) / 2.0
        for position in order[start:end]:
            ranks[position] = average_rank
        start = end
    return ranks


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    denom = math.sqrt(sum(x * x for x in dx) * sum(y * y for y in dy))
    if denom == 0:
        return None
    return sum(x * y for x, y in zip(dx, dy)) / denom


def spearman(xs: list[float], ys: list[float]) -> float | None:
    return pearson(rankdata(xs), rankdata(ys))


def metric_values(predictions: list[float], references: list[float]) -> dict[str, float | None]:
    if not predictions or len(predictions) != len(references):
        return {key: None for key in ("mae_eV", "rmse_eV", "mean_error_eV", "median_abs_error_eV", "max_abs_error_eV", "spearman", "min_pred_eV", "max_pred_eV")}
    errors = [pred - ref for pred, ref in zip(predictions, references)]
    absolute = [abs(value) for value in errors]
    return {
        "mae_eV": sum(absolute) / len(absolute),
        "rmse_eV": math.sqrt(sum(value * value for value in errors) / len(errors)),
        "mean_error_eV": sum(errors) / len(errors),
        "median_abs_error_eV": median(absolute),
        "max_abs_error_eV": max(absolute),
        "spearman": spearman(predictions, references),
        "min_pred_eV": min(predictions),
        "max_pred_eV": max(predictions),
    }
