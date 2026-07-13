#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import json
import math
from pathlib import Path

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    from ase.data import covalent_radii
    from ase.data.colors import jmol_colors
    from ase.formula import Formula
    from ase.io import read
except ImportError as exc:
    raise SystemExit(
        "Preview rendering requires ASE, NumPy, and Pillow. "
        "Activate the AutoCata conda environment before running this script. "
        f"Missing dependency: {exc}"
    ) from exc


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRUE_VALUES = {"1", "true", "yes", "y"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Render headless PNG and rotating GIF previews from accepted XYZ structures."
    )
    parser.add_argument("--success-summary", required=True, help="Accepted-structure CSV file.")
    parser.add_argument("--output-dir", required=True, help="Preview output directory.")
    parser.add_argument(
        "--shared-material-summary",
        help="Optional reaction-network CSV; only materials with all_adsorbates_passed=true are rendered.",
    )
    parser.add_argument("--adsorbate", default="", help="Fallback adsorbate label for single-adsorbate runs.")
    parser.add_argument("--max-structures", type=int, default=5)
    parser.add_argument("--frames", type=int, default=12)
    parser.add_argument("--image-width", type=int, default=720)
    parser.add_argument("--image-height", type=int, default=540)
    parser.add_argument("--gif-duration-ms", type=int, default=160)
    parser.add_argument("--no-gif", action="store_true", help="Generate PNG files only.")
    return parser.parse_args()


def resolve_project_path(path):
    path = Path(path)
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


def path_for_message(path):
    path = Path(path)
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def read_csv(path):
    if not path or not Path(path).is_file():
        return []
    with open(path, "r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def safe_name(value):
    chars = []
    for char in str(value):
        chars.append(char if char.isalnum() or char in "-_." else "_")
    return "".join(chars).strip("_") or "structure"


def energy_value(row):
    try:
        return float(row.get("E_pred", ""))
    except (TypeError, ValueError):
        return float("inf")


def accepted_row(row):
    value = str(row.get("passed", "")).strip().lower()
    return not value or value in TRUE_VALUES


def allowed_shared_materials(summary_path):
    if not summary_path:
        return None
    rows = read_csv(summary_path)
    return {
        row.get("material", "")
        for row in rows
        if str(row.get("all_adsorbates_passed", "")).strip().lower() in TRUE_VALUES
    }


def source_xyz_path(row):
    value = row.get("success_xyz_file") or row.get("xyz_file") or row.get("source_xyz_file")
    return resolve_project_path(value) if value else None


def select_rows(rows, max_structures, fallback_adsorbate, shared_materials=None):
    if max_structures <= 0:
        return []

    candidates = []
    for index, row in enumerate(rows):
        if not accepted_row(row):
            continue
        if shared_materials is not None and row.get("material", "") not in shared_materials:
            continue
        source = source_xyz_path(row)
        if source is None or not source.is_file():
            continue
        copy = dict(row)
        copy["_source"] = source
        copy["_input_index"] = index
        copy["_adsorbate"] = row.get("adsorbate") or fallback_adsorbate
        candidates.append(copy)

    candidates.sort(key=lambda row: (energy_value(row), row["_input_index"]))
    selected = []
    selected_ids = set()
    seen_groups = set()

    for row in candidates:
        group = (row.get("material", ""), row.get("_adsorbate", ""))
        if group in seen_groups:
            continue
        selected.append(row)
        selected_ids.add(row["_input_index"])
        seen_groups.add(group)
        if len(selected) >= max_structures:
            return selected

    for row in candidates:
        if row["_input_index"] in selected_ids:
            continue
        selected.append(row)
        if len(selected) >= max_structures:
            break
    return selected


def load_font(size, bold=False):
    names = ["DejaVuSans-Bold.ttf", "Arial Bold.ttf"] if bold else ["DejaVuSans.ttf", "Arial.ttf"]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def rotate_positions(positions, azimuth_deg, tilt_deg=58.0):
    azimuth = math.radians(azimuth_deg)
    tilt = math.radians(tilt_deg)
    rz = np.array(
        [
            [math.cos(azimuth), -math.sin(azimuth), 0.0],
            [math.sin(azimuth), math.cos(azimuth), 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    rx = np.array(
        [
            [1.0, 0.0, 0.0],
            [0.0, math.cos(tilt), -math.sin(tilt)],
            [0.0, math.sin(tilt), math.cos(tilt)],
        ]
    )
    return positions @ rz.T @ rx.T


def atom_count_from_formula(formula):
    if not formula:
        return 0
    try:
        return sum(Formula(str(formula)).count().values())
    except (ValueError, TypeError):
        return 0


def bond_pairs(positions, numbers):
    pairs = []
    for left in range(len(positions)):
        for right in range(left + 1, len(positions)):
            distance = float(np.linalg.norm(positions[left] - positions[right]))
            cutoff = 1.15 * (covalent_radii[numbers[left]] + covalent_radii[numbers[right]])
            if 0.3 < distance <= cutoff:
                pairs.append((left, right))
    return pairs


def lighten(rgb, fraction):
    return tuple(int(channel + (255 - channel) * fraction) for channel in rgb)


def darken(rgb, fraction):
    return tuple(int(channel * (1.0 - fraction)) for channel in rgb)


def row_label(row):
    sample = row.get("sample_id") or Path(row["_source"]).stem
    material = row.get("material") or "unknown material"
    adsorbate = row.get("_adsorbate") or "unknown adsorbate"
    energy = energy_value(row)
    energy_text = f"E_pred {energy:.3f} eV" if math.isfinite(energy) else "E_pred n/a"
    return sample, f"{material} | {adsorbate} | {energy_text}"


def render_frame(atoms, row, azimuth_deg, width, height):
    background = (247, 249, 247)
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    title_font = load_font(20, bold=True)
    meta_font = load_font(15)
    small_font = load_font(13)

    positions = atoms.get_positions().astype(float)
    positions -= positions.mean(axis=0)
    transformed = rotate_positions(positions, azimuth_deg)
    numbers = atoms.get_atomic_numbers()
    atomic_radii = np.array([max(0.45, covalent_radii[number]) for number in numbers])

    left, right = 36, width - 36
    top, bottom = 78, height - 48
    span_x = max(1.0, float(np.ptp(transformed[:, 0]) + 2.0 * atomic_radii.max()))
    span_y = max(1.0, float(np.ptp(transformed[:, 1]) + 2.0 * atomic_radii.max()))
    scale = min((right - left) / span_x, (bottom - top) / span_y)
    scale *= 0.92
    screen_x = width / 2.0 + transformed[:, 0] * scale
    screen_y = (top + bottom) / 2.0 - transformed[:, 1] * scale
    screen_radii = np.clip(atomic_radii * scale * 0.55, 5.0, 24.0)

    sample, metadata = row_label(row)
    draw.text((20, 15), sample, fill=(28, 38, 35), font=title_font)
    draw.text((20, 44), metadata, fill=(73, 86, 81), font=meta_font)
    draw.rounded_rectangle((12, 8, width - 12, height - 12), radius=8, outline=(205, 214, 207), width=1)

    bonds = bond_pairs(positions, numbers)
    for left_index, right_index in sorted(
        bonds,
        key=lambda pair: (transformed[pair[0], 2] + transformed[pair[1], 2]) / 2.0,
    ):
        draw.line(
            (
                float(screen_x[left_index]),
                float(screen_y[left_index]),
                float(screen_x[right_index]),
                float(screen_y[right_index]),
            ),
            fill=(151, 158, 155),
            width=max(2, int(scale * 0.055)),
        )

    adsorbate_formula = row.get("adsorbate_formula") or row.get("_adsorbate", "")
    n_adsorbate = min(len(atoms), atom_count_from_formula(adsorbate_formula))
    adsorbate_start = len(atoms) - n_adsorbate

    for index in np.argsort(transformed[:, 2]):
        number = numbers[index]
        rgb = tuple(int(round(channel * 255)) for channel in jmol_colors[number])
        radius = float(screen_radii[index])
        x = float(screen_x[index])
        y = float(screen_y[index])
        box = (x - radius, y - radius, x + radius, y + radius)
        draw.ellipse(box, fill=darken(rgb, 0.22), outline=(56, 61, 59), width=1)
        inner = radius * 0.88
        draw.ellipse(
            (x - inner, y - inner, x + inner, y + inner),
            fill=rgb,
        )
        highlight = radius * 0.34
        draw.ellipse(
            (x - radius * 0.48, y - radius * 0.50, x - radius * 0.48 + highlight, y - radius * 0.50 + highlight),
            fill=lighten(rgb, 0.62),
        )
        if n_adsorbate and index >= adsorbate_start:
            halo = radius + 3
            draw.ellipse((x - halo, y - halo, x + halo, y + halo), outline=(13, 145, 102), width=3)

    if n_adsorbate:
        draw.text((20, height - 36), "Adsorbate atoms outlined in green", fill=(13, 120, 87), font=small_font)
    return image


def render_row(row, rank, output_dir, args):
    atoms = read(row["_source"], format="xyz")
    sample = row.get("sample_id") or Path(row["_source"]).stem
    adsorbate = row.get("_adsorbate") or "adsorbate"
    material = row.get("material") or "material"
    stem = f"{rank:03d}_{safe_name(sample)}_{safe_name(material)}_{safe_name(adsorbate)}"
    png_path = output_dir / f"{stem}.png"
    gif_path = output_dir / f"{stem}.gif"

    poster = render_frame(atoms, row, 30.0, args.image_width, args.image_height)
    poster.save(png_path, format="PNG", optimize=True)

    if not args.no_gif:
        frame_count = max(4, args.frames)
        frames = [
            render_frame(atoms, row, 360.0 * index / frame_count, args.image_width, args.image_height)
            for index in range(frame_count)
        ]
        frames[0].save(
            gif_path,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=max(40, args.gif_duration_ms),
            loop=0,
            optimize=True,
            disposal=2,
        )

    return png_path, None if args.no_gif else gif_path, len(atoms)


def main():
    args = parse_args()
    success_summary = resolve_project_path(args.success_summary)
    output_dir = resolve_project_path(args.output_dir)
    shared_summary = resolve_project_path(args.shared_material_summary) if args.shared_material_summary else None
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = read_csv(success_summary)
    shared_materials = allowed_shared_materials(shared_summary)
    selected = select_rows(rows, max(0, args.max_structures), args.adsorbate, shared_materials)
    preview_rows = []

    for rank, row in enumerate(selected, start=1):
        result = {
            "rank": rank,
            "sample_id": row.get("sample_id") or Path(row["_source"]).stem,
            "adsorbate": row.get("_adsorbate", ""),
            "material": row.get("material", ""),
            "E_pred": row.get("E_pred", ""),
            "xyz_file": path_for_message(row["_source"]),
            "png_file": "",
            "gif_file": "",
            "natoms": row.get("natoms") or row.get("n_atoms", ""),
            "status": "success",
            "error": "",
        }
        try:
            png_path, gif_path, natoms = render_row(row, rank, output_dir, args)
            result["png_file"] = path_for_message(png_path)
            result["gif_file"] = path_for_message(gif_path) if gif_path else ""
            result["natoms"] = natoms
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)
        preview_rows.append(result)

    fields = [
        "rank",
        "sample_id",
        "adsorbate",
        "material",
        "E_pred",
        "xyz_file",
        "png_file",
        "gif_file",
        "natoms",
        "status",
        "error",
    ]
    summary_path = output_dir / "preview_summary.csv"
    manifest_path = output_dir / "preview_manifest.json"
    write_csv(summary_path, fields, preview_rows)
    write_json(
        manifest_path,
        {
            "source_summary": path_for_message(success_summary),
            "shared_material_summary": path_for_message(shared_summary) if shared_summary else "",
            "selected_count": len(selected),
            "rendered_count": sum(row["status"] == "success" for row in preview_rows),
            "previews": preview_rows,
        },
    )

    failed_count = sum(row["status"] != "success" for row in preview_rows)
    print(f"[done] previews={len(preview_rows) - failed_count}/{len(preview_rows)}")
    print(f"[done] summary={path_for_message(summary_path)}")
    print(f"[done] manifest={path_for_message(manifest_path)}")


if __name__ == "__main__":
    main()
