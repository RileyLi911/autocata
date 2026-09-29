#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import json
import shutil
from pathlib import Path

from structure_view import load_view, project_view


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args():
    parser = argparse.ArgumentParser(description="Build a browser-based 3D viewer for accepted XYZ structures.")
    parser.add_argument("--success-summary", required=True, help="Path to workflow success_summary.csv.")
    parser.add_argument("--output-dir", required=True, help="Viewer output directory.")
    parser.add_argument("--title", default="Accepted Structures", help="Viewer page title.")
    parser.add_argument("--adsorbate", default="", help="Fallback formula for legacy single-adsorbate CSV files.")
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


def read_rows(path):
    if not path.is_file():
        return []
    with open(path, "r", newline="", encoding="utf-8") as fr:
        return list(csv.DictReader(fr))


def safe_name(value):
    keep = []
    for char in str(value):
        if char.isalnum() or char in ["-", "_", "."]:
            keep.append(char)
        else:
            keep.append("_")
    return "".join(keep).strip("_") or "structure"


def copy_structures(rows, structures_dir, adsorbate=""):
    structures_dir.mkdir(parents=True, exist_ok=True)
    manifest = []

    for idx, row in enumerate(rows, start=1):
        source_path = row.get("success_xyz_file") or row.get("xyz_file")
        if not source_path:
            continue

        source = resolve_project_path(source_path)
        if not source.is_file():
            continue

        sample_id = row.get("sample_id") or f"sample_{idx}"
        destination_name = f"{idx:04d}_{safe_name(sample_id)}.xyz"
        destination = structures_dir / destination_name
        shutil.copy2(source, destination)
        xyz_text = source.read_text(encoding="utf-8")
        atoms, mask = load_view(source, {"_adsorbate": adsorbate, **row})
        positions = project_view(atoms.positions, mask, 30.0)
        display_xyz = f"{len(atoms)}\nOriented display copy\n" + "\n".join(
            f"{symbol} {x:.12f} {y:.12f} {z:.12f}"
            for symbol, (x, y, z) in zip(atoms.get_chemical_symbols(), positions)
        ) + "\n"

        manifest.append(
            {
                "id": sample_id,
                "round": row.get("round", ""),
                "file": f"structures/{destination_name}",
                "source_xyz_file": path_for_message(source),
                "material": row.get("material", ""),
                "material_elements": row.get("material_elements", ""),
                "adsorbate_valid": row.get("adsorbate_valid", ""),
                "E_pred": row.get("E_pred", ""),
                "F_rms": row.get("F_rms", ""),
                "F_max": row.get("F_max", ""),
                "natoms": row.get("natoms", ""),
                "xyz": xyz_text,
                "display_xyz": display_xyz,
            }
        )

    return manifest


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as fw:
        json.dump(data, fw, indent=2)


def write_html(path, title, manifest):
    # Script elements are raw text: HTML entities would corrupt JSON strings.
    embedded_data = json.dumps(manifest).replace("<", "\\u003c")
    html_template = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <script src="https://3Dmol.org/build/3Dmol-min.js"></script>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f7f7f3;
      --surface: #ffffff;
      --text: #1e2329;
      --muted: #65707d;
      --line: #d8ddd2;
      --accent: #0c7c59;
      --accent-strong: #075c42;
      --danger: #b74434;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #111418;
        --surface: #1a1f24;
        --text: #edf0f2;
        --muted: #a8b0b8;
        --line: #303841;
        --accent: #43c59e;
        --accent-strong: #8ae3c5;
        --danger: #ff8a76;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    main {
      min-height: 100vh;
      display: grid;
      grid-template-columns: minmax(280px, 360px) minmax(0, 1fr);
    }
    aside {
      border-right: 1px solid var(--line);
      background: var(--surface);
      min-width: 0;
      display: flex;
      flex-direction: column;
    }
    header {
      padding: 18px 18px 12px;
      border-bottom: 1px solid var(--line);
    }
    h1 {
      margin: 0;
      font-size: 19px;
      line-height: 1.25;
      font-weight: 720;
    }
    .subtle {
      margin-top: 6px;
      color: var(--muted);
      font-size: 13px;
    }
    .list {
      overflow: auto;
      padding: 10px;
      display: grid;
      gap: 8px;
    }
    .item {
      width: 100%;
      text-align: left;
      border: 1px solid var(--line);
      background: transparent;
      color: inherit;
      border-radius: 8px;
      padding: 10px;
      cursor: pointer;
    }
    .item:hover, .item.active {
      border-color: var(--accent);
      outline: 2px solid color-mix(in srgb, var(--accent) 20%, transparent);
    }
    .item-title {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      font-size: 14px;
      font-weight: 680;
    }
    .item-meta {
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .viewer-wrap {
      min-width: 0;
      display: grid;
      grid-template-rows: auto minmax(360px, 1fr);
    }
    .toolbar {
      min-width: 0;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: var(--surface);
    }
    .details {
      min-width: 0;
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      color: var(--muted);
      font-size: 13px;
    }
    .details strong {
      color: var(--text);
      font-weight: 680;
    }
    .controls {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    button.control, a.control {
      display: inline-flex;
      align-items: center;
      min-height: 34px;
      border: 1px solid var(--line);
      background: transparent;
      color: var(--text);
      border-radius: 8px;
      padding: 7px 10px;
      font-size: 13px;
      text-decoration: none;
      cursor: pointer;
    }
    button.control.active {
      border-color: var(--accent);
      color: var(--accent-strong);
      font-weight: 680;
    }
    #viewer {
      width: 100%;
      min-height: 360px;
      height: 100%;
      position: relative;
    }
    .empty {
      padding: 20px;
      color: var(--muted);
    }
    .error {
      padding: 20px;
      color: var(--danger);
    }
    @media (max-width: 860px) {
      main {
        grid-template-columns: 1fr;
        grid-template-rows: minmax(180px, 36vh) minmax(420px, 64vh);
      }
      aside {
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }
      .list {
        grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
      }
      .viewer-wrap {
        min-height: 420px;
      }
    }
  </style>
</head>
<body>
  <main>
    <aside>
      <header>
        <h1>__TITLE__</h1>
        <div class="subtle" id="count">Loading structures...</div>
      </header>
      <section class="list" id="structureList"></section>
    </aside>
    <section class="viewer-wrap">
      <div class="toolbar">
        <div class="details" id="details"></div>
        <div class="controls">
          <button class="control active" data-style="stick">Stick</button>
          <button class="control" data-style="sphere">Sphere</button>
          <button class="control" data-style="line">Line</button>
          <button class="control" id="resetView">Reset</button>
          <a class="control" id="downloadLink" href="#" download>Download XYZ</a>
        </div>
      </div>
      <div id="viewer"></div>
    </section>
  </main>

  <script id="structureData" type="application/json">__STRUCTURE_DATA__</script>
  <script>
    const state = { structures: [], activeIndex: 0, style: "stick", viewer: null };
    const listEl = document.getElementById("structureList");
    const detailsEl = document.getElementById("details");
    const countEl = document.getElementById("count");
    const downloadLink = document.getElementById("downloadLink");

    function fmt(value, digits = 4) {
      if (value === undefined || value === null || value === "") return "n/a";
      const num = Number(value);
      if (!Number.isFinite(num)) return String(value);
      return num.toFixed(digits);
    }

    function itemLabel(item) {
      const material = item.material || "unknown material";
      const energy = fmt(item.E_pred, 3);
      return `${material} · E ${energy}`;
    }

    function renderList() {
      listEl.innerHTML = "";
      countEl.textContent = `${state.structures.length} accepted structures`;
      if (state.structures.length === 0) {
        listEl.innerHTML = '<div class="empty">No accepted structures were found in success_summary.csv.</div>';
        detailsEl.innerHTML = "";
        return;
      }
      state.structures.forEach((item, index) => {
        const button = document.createElement("button");
        button.className = `item${index === state.activeIndex ? " active" : ""}`;
        button.type = "button";
        button.innerHTML = `
          <div class="item-title"><span>${item.id}</span><span>${fmt(item.E_pred, 3)}</span></div>
          <div class="item-meta">${item.material || "unknown"} · ${item.natoms || "?"} atoms · ${item.round || ""}</div>
        `;
        button.addEventListener("click", () => loadStructure(index));
        listEl.appendChild(button);
      });
    }

    function applyStyle() {
      state.viewer.setStyle({});
      if (state.style === "sphere") {
        state.viewer.setStyle({}, { sphere: { scale: 0.35 } });
      } else if (state.style === "line") {
        state.viewer.setStyle({}, { line: { linewidth: 2 } });
      } else {
        state.viewer.setStyle({}, { stick: { radius: 0.18 }, sphere: { scale: 0.22 } });
      }
      state.viewer.zoomTo();
      state.viewer.render();
    }

    async function loadStructure(index) {
      state.activeIndex = index;
      renderList();
      const item = state.structures[index];
      detailsEl.innerHTML = `
        <span><strong>${item.id}</strong></span>
        <span>Material <strong>${item.material || "n/a"}</strong></span>
        <span>E <strong>${fmt(item.E_pred)}</strong></span>
        <span>F RMS <strong>${fmt(item.F_rms)}</strong></span>
        <span>F max <strong>${fmt(item.F_max)}</strong></span>
      `;
      downloadLink.href = item.file;
      downloadLink.download = `${item.id}.xyz`;
      const xyz = item.xyz || "";
      if (!xyz) {
        document.getElementById("viewer").innerHTML = '<div class="error">XYZ data is missing.</div>';
        return;
      }
      const blob = new Blob([xyz], { type: "chemical/x-xyz" });
      if (downloadLink.dataset.url) {
        URL.revokeObjectURL(downloadLink.dataset.url);
      }
      const blobUrl = URL.createObjectURL(blob);
      downloadLink.href = blobUrl;
      downloadLink.dataset.url = blobUrl;
      state.viewer.clear();
      state.viewer.addModel(item.display_xyz || xyz, "xyz");
      state.viewer.setView([0, 0, 0, 0, 0, 0, 0, 1]);
      applyStyle();
    }

    function setStyle(style) {
      state.style = style;
      document.querySelectorAll("[data-style]").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.style === style);
      });
      if (state.viewer) applyStyle();
    }

    async function init() {
      if (!window.$3Dmol) {
        document.getElementById("viewer").innerHTML = '<div class="error">3Dmol.js did not load. Check network access to https://3Dmol.org.</div>';
        return;
      }
      state.viewer = $3Dmol.createViewer("viewer", { backgroundColor: "white" });
      document.querySelectorAll("[data-style]").forEach(btn => {
        btn.addEventListener("click", () => setStyle(btn.dataset.style));
      });
      document.getElementById("resetView").addEventListener("click", () => {
        state.viewer.setView([0, 0, 0, 0, 0, 0, 0, 1]);
        state.viewer.zoomTo();
        state.viewer.render();
      });

      state.structures = JSON.parse(document.getElementById("structureData").textContent || "[]");
      renderList();
      if (state.structures.length > 0) {
        await loadStructure(0);
      }
    }

    init().catch(error => {
      document.getElementById("viewer").innerHTML = `<div class="error">${error.message}</div>`;
    });
  </script>
</body>
</html>
"""
    path.write_text(
        html_template.replace("__TITLE__", title).replace("__STRUCTURE_DATA__", embedded_data),
        encoding="utf-8",
    )


def main():
    args = parse_args()
    success_summary = resolve_project_path(args.success_summary)
    output_dir = resolve_project_path(args.output_dir)
    structures_dir = output_dir / "structures"

    rows = read_rows(success_summary)
    manifest = copy_structures(rows, structures_dir, args.adsorbate)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "manifest.json", manifest)
    write_html(output_dir / "index.html", args.title, manifest)

    print(f"[done] viewer={path_for_message(output_dir / 'index.html')}")
    print(f"[done] structures={len(manifest)}")


if __name__ == "__main__":
    main()
