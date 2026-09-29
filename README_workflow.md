# Agent-Ready Workflow

This project can now run the structure-generation pipeline from a single
workflow config, without using an external agent framework.

All user-facing reports should be written in English by default.

The intended future pattern is:

1. A natural-language agent converts a user request into `config/workflow_config.yml`
   or `config/reaction_workflow_config.yml`.
2. The local runner executes the existing CLI tools.
3. The runner writes machine-readable CSV/JSON outputs for the agent to inspect.

Example user request:

```text
I want CH3 as the adsorbate, Au-based catalyst materials, MLP energy < 0,
and 5 successful structures.
```

Energy-window requests are supported directly:

```text
Run CH3 screening with -1.5 < E_pred < -0.5.
```

Once an agent has read `SKILL.md`, `AGENTS.md`, this file, and
`config/adsorbates.yml`, short user prompts are enough. Examples:

```text
Run CH3 screening on unrestricted materials. Use CUDA, E_pred < 0, target 1000.
```

```text
Run CH3 screening on Cu-based catalysts. Use CUDA, E_pred < 0, target 500.
```

```text
For CO2RR, choose available intermediates from the adsorbate registry and run a
reaction-network search for one shared material.
```

Equivalent workflow config fields:

```yaml
workflow:
  adsorbate: "CH3"
  target_success_count: 5
  material_query:
    mode: "contains"
    elements: ["Au"]
  filters:
    require_adsorbate_valid: true
    mlp_energy_gt: null
    mlp_energy_lt: 0.0
```

For an energy window:

```yaml
workflow:
  filters:
    mlp_energy_gt: -1.5
    mlp_energy_lt: -0.5
```

## Runner

Dry-run the planned commands:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml --dry-run
```

If `workflow.run_name` is set, dry-runs are written to a timestamped dry-run
directory so that they do not pollute the production run directory.

Run the MVP workflow:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml
```

The runner refuses to write into an existing non-empty run directory by default.
Use a unique `workflow.run_name` for production runs. If a deliberate rerun is
needed, pass:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml --overwrite-run-dir
```

Use `--allow-existing-run-dir` only for debugging because stale `rounds/`
subdirectories can confuse manual inspection.

Override the adsorbate:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml --adsorbate CH3
```

## Agent Setup Check

Before running workflows on a new machine:

```bash
conda activate autocata
```

If the environment does not exist:

```bash
conda env create -f envs/autocata.yml
conda activate autocata
```

Download or validate the required model assets:

```bash
python script/download_models.py
python script/download_models.py --check-only
```

Then run at least one dry-run:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml --dry-run
```

By default, the workflow first looks up the adsorbate in:

```text
config/adsorbates.yml
```

That registry maps adsorbates to finetuned checkpoints under:

```text
data/finetune_model/checkpoint-{adsorbate}
```

If an adsorbate is not listed in the registry, the runner falls back to:

```text
outputs/finetune_{adsorbate}/checkpoint-*
```

The workflow does not launch training unless:

```yaml
workflow:
  fine_tune:
    enabled: true
```

## Node Order

Each round runs these existing CLI nodes:

```text
script/generate.py
script/ASE_check_manual.py
script/index_xyz_metadata.py
MLP_check/OC20_MLP/mlp_scores.py
```

The runner then filters rows by:

- `adsorbate_valid == true`
- material query match
- `E_pred > workflow.filters.mlp_energy_gt` when the lower bound is set
- `E_pred < workflow.filters.mlp_energy_lt` when the upper bound is set
- optional duplicate check

## Reaction-Network Runner

For reactions with multiple key intermediates, use the reaction-network runner.
The recommended mode is `material_first`: generate and parse large candidate
sets first, identify catalyst materials that appear for all selected
adsorbates, and run MLP only on those shared-material candidates. This avoids
spending MLP time on materials that cannot become reaction-network hits.

The legacy `mlp_first` mode is still available for comparison. It reuses the
single-adsorbate workflow for each adsorbate, then aggregates accepted
structures by catalyst material.

Dry-run:

```bash
python reaction_workflow_runner.py --reaction-config config/reaction_workflow_config.yml --dry-run
```

Run:

```bash
python reaction_workflow_runner.py --reaction-config config/reaction_workflow_config.yml
```

Override adsorbates from the command line:

```bash
python reaction_workflow_runner.py --reaction-config config/reaction_workflow_config.yml --adsorbates CH3,CHO,OH
```

The reaction config controls:

```yaml
reaction_workflow:
  screening_mode: "material_first"
  adsorbate_registry: "config/adsorbates.yml"
  adsorbates: ["CH3", "CHO", "OH"]
  target_shared_material_count: 1
  shared_material:
    target_success_per_adsorbate_per_material: 1
  material_first:
    target_shared_material_candidates: null
    min_candidates_per_adsorbate_per_material: 1
    max_shared_materials_for_mlp: 50
    max_structures_per_adsorbate_per_material: 20
```

Each child adsorbate run is written under:

```text
outputs/reaction_workflows/{run_name}/adsorbate_runs/{adsorbate}/
```

The main reaction-level outputs are:

```text
reaction_workflow_report.json
adsorbate_run_summary.csv
pre_mlp_candidates.csv
pre_mlp_shared_material_summary.csv
material_adsorbate_precheck_matrix.csv
targeted_mlp_inputs.csv
all_mlp_scored_candidates.csv
all_success_structures.csv
shared_material_summary.csv
material_adsorbate_matrix.csv
reaction_run.log
```

In `material_first` mode, use `pre_mlp_shared_material_summary.csv` to inspect
materials found before MLP. Use `targeted_mlp_inputs.csv` and
`all_mlp_scored_candidates.csv` to audit which shared-material structures were
actually sent to MLP.

In `material_first` mode, the pre-MLP generation budget is controlled by:

```text
per_adsorbate.max_rounds * per_adsorbate.generation_per_round
```

`per_adsorbate.target_success_count` is kept for the legacy `mlp_first` mode.

Production reaction-network runs should not use smoke-test budgets. Unless the
user explicitly asks for a small test/debug run, start with at least 100,000
generated structures per adsorbate:

```yaml
per_adsorbate:
  max_rounds: 10
  generation_per_round: 10000
material_first:
  target_shared_material_candidates: 100
  max_shared_materials_for_mlp: 100
  max_structures_per_adsorbate_per_material: 50
```

If no shared material is found after only hundreds or a few thousand generated
structures per adsorbate, treat the result as undersampled rather than a
negative chemical conclusion.

`shared_material_summary.csv` ranks materials by how many target adsorbates
they cover. `material_adsorbate_matrix.csv` is the most useful table for an
agent: it has one row per material and per-adsorbate success counts/best
energies, so the next step can focus on missing adsorbate-material pairs.

## Outputs

Runs are written under:

```text
outputs/workflows/{run_name}/
```

Important outputs:

```text
success_structures/
viewer/index.html
structure_previews/
all_candidates.csv
success_summary.csv
failure_summary.csv
workflow_report.json
run.log
rounds/
```

Reaction-network runs are written under:

```text
outputs/reaction_workflows/{run_name}/
```

`workflow_report.json` is the main machine-readable status file.

`all_candidates.csv` contains one row per MLP-scored structure with:

```text
round, xyz_file, sample_id, E_pred, F_rms, F_max,
material, material_elements, adsorbate_valid,
passed, failure_reason, error
```

For statistics, trust `workflow_report.json`, `all_candidates.csv`, and
`success_summary.csv`. Do not count directories under `rounds/` as completed
rounds if a run directory was reused.

Energy filters are applied before rows are copied to `success_summary.csv`.
For example, `mlp_energy_gt: -1.5` and `mlp_energy_lt: -0.5` means accepted
structures satisfy `-1.5 < E_pred < -0.5`.

`viewer/index.html` is a browser-based 3D gallery for accepted structures. It
uses 3Dmol.js and embeds accepted XYZ text into the HTML, so it can be opened
directly from the local filesystem without a separate web server.

On the workflow host, open it directly:

```bash
start outputs/workflows/{run_name}/viewer/index.html
```

To open the viewer automatically after a local workflow run, use:

```bash
python workflow_runner.py --workflow-config config/workflow_config.yml --open-viewer
```

or set:

```yaml
workflow:
  viewer:
    auto_open: true
```

`structure_previews/` contains headless renderings that do not require a
browser or display server:

```text
preview_summary.csv
preview_manifest.json
*.png
*.gif
```

The PNG is a representative view and the GIF rotates the structure around the
surface-normal axis. Adsorbate atoms are outlined in green. Configure the
number and size of previews with:

```yaml
workflow:
  preview:
    enabled: true
    max_structures: 5
    frames: 12
    image_width: 720
    image_height: 540
    gif_duration_ms: 160
    gif: true
```

Previews and the browser gallery share `script/structure_view.py`. ASE reads
extended XYZ cell/PBC metadata. Adsorbates are identified from OC20 tags (2),
or from the formula-validated trailing atoms used by AutoCata. The slab normal
comes from the nonperiodic direction of a 2D-periodic cell, the largest slab
vacuum gap of a fully periodic cell, or a best-fit plane for cell-free XYZ.
Periodic display images are made contiguous before the adsorption side is
chosen. A proper rotation places that side above the slab; the original XYZ
and downloaded coordinates are unchanged. GIF frames rotate about this normal.
The usual 58-degree tilt is increased toward a side view when lateral offsets
would otherwise place the adsorbate below the slab in projection. Colors,
image sizes, atom radii and labels are unchanged.

The guarantee applies to an identifiable slab with adsorbates on one side.
For adsorbates overlapping the slab's height range, the projected adsorbate
center is kept above the slab center; a rigid rotation cannot make embedded
atoms or adsorbates on opposite surfaces all lie above every slab atom.
Missing identity or a centered, ambiguous adsorption side raises an explicit
error (recorded per structure in the preview manifest). Plain XYZ cannot recover
periodic information that was not saved: use extended XYZ for wrapped systems.
The browser initial view and Reset use the same orientation; users can still
rotate the structure freely afterward.

To re-render existing results without generation, inference or MLP, use a new
output directory and supply the formula for older single-adsorbate CSV files:

```bash
conda activate autocata
python script/render_xyz_preview.py --success-summary outputs/workflows/RUN/success_summary.csv --adsorbate CH3 --output-dir outputs/workflows/VIEW_CHECK/structure_previews --max-structures 3
python script/build_structure_gallery.py --success-summary outputs/workflows/RUN/success_summary.csv --adsorbate CH3 --output-dir outputs/workflows/VIEW_CHECK/viewer
python -m unittest discover -s tests -v
```

Replace `RUN`, `VIEW_CHECK` and `CH3` with the existing run, a fresh output
directory and the actual adsorbate formula. These two rendering commands never
invoke the workflow runners. The lightweight tests exercise rotated slabs,
multiple faces/thicknesses/species, bottom adsorption, wrapped/skew cells,
distance preservation, PNG/GIF output and gallery serialization.

For reaction-network runs, the same keys live under
`reaction_workflow.preview`. Only materials marked
`all_adsorbates_passed: true` are selected for reaction-network previews.
Selection prefers different material and adsorbate combinations, then lower
`E_pred` values.

An agent replying through Feishu should read `preview_manifest.json` and attach
the first successful GIF files. If animated GIF attachment is unavailable, it
should attach the corresponding PNG files instead. AutoCata does not store
Feishu credentials or call the Feishu API directly.

## Failure Reasons

The runner currently records:

```text
parse_failed
wrong_adsorbate
wrong_material
mlp_error
mlp_energy_below_min
mlp_energy_above_max
duplicate
```

Future geometry checks can add:

```text
geometry_failed
```

## Material Queries

Exact material:

```yaml
material_query:
  mode: "exact"
  material: "Au-Pd"
```

Au-based materials:

```yaml
material_query:
  mode: "contains"
  elements: ["Au"]
```

Any material containing Au or Pd:

```yaml
material_query:
  mode: "any_contains"
  elements: ["Au", "Pd"]
```

No material filter:

```yaml
material_query:
  mode: "any"
```

## Common Short Requests

Broad single-adsorbate production:

```text
Run CH3 production screening on unrestricted materials. Use CUDA, E_pred < 0,
target 1000 accepted structures, and dry-run first.
```

Energy-window production:

```text
Run CH3 screening on unrestricted materials. Use CUDA, -1.5 < E_pred < -0.5,
target 1000 accepted structures, and dry-run first.
```

Element-based production:

```text
Run CH3 screening on Au-based catalysts. Use CUDA, E_pred < 0, target 500, and
dry-run first.
```

Exact-material production:

```text
Run CH3 screening on exact Au-Pd catalysts. Use CUDA, E_pred < 0, target 100,
and dry-run first.
```

Reaction-network search:

```text
Run a CO2RR reaction-network search using available registry adsorbates. Find at
least one shared material and report missing adsorbate-material gaps if none are
found.
```

Material names come from `index_xyz_metadata.py`, where element sets are sorted
alphabetically. For example, `Pd-Au` and `Au-Pd` both become `Au-Pd`.

## MVP Limits

- The runner is a local orchestrator, not an external agent framework.
- It does not parse natural language itself.
- It assumes the existing CLI tools are installed and runnable in the current Python environment.
- Fine-tuning remains optional because it is expensive.
- Duplicate checking is byte-level XYZ matching when enabled; structural
  equivalence checks can be added later.
