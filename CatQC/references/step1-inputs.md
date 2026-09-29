# Step 1: Discover Inputs And Confirm Configuration

Step 1 is read-only for source data. Inspect inputs and write derived
configuration artifacts only.

Require Step 0 to have resolved `benchmark_root` and written
`run_manifest.json`. Search for source inputs only in the manifest's
`source_search_roots`, excluding `skill_root` and the run base.

## Parse The Benchmark Request

Before locating files, extract the start-request parameters defined in
`SKILL.md`:

- `model_selection_mode`: `matbench_top_n` or `user_specified`.
- Positive integer `model_count` for `matbench_top_n`, or an ordered confirmed
  `requested_models` list for `user_specified`.
- User-provided `dataset_name` or dataset path.
- One or both values in `requested_tasks`: `single_point`, `relaxation`.

Ask only for parameters that are absent or ambiguous. Preserve the user's
original wording in `benchmark_request.original_prompt`. Use `dataset_name` to
find likely inputs, but do not infer scientific definitions from the name alone.
For `user_specified`, verify that every requested model has completed the
preflight in `references/request-model-selection.md`; do not reopen identity
selection from package installation results.

## Locate Inputs

Required inputs:

- Structure input: a multi-frame `.xyz`/`.extxyz`, a single `.cif`,
  `POSCAR`/`CONTCAR`, or a VASP-format `.vasp` file, or a directory of supported structure files. Directory
  entries are sorted by case-insensitive filename and become consecutive
  zero-based structure frames.
- For `dft_reference_mode=provided`, a DFT reference table: `.csv`, `.tsv`,
  `.xlsx`, or `.xls`.
- For `dft_reference_mode=generate_vasp`, no input adsorption-energy table is
  required. Confirm adsorbate indices and freeze the DFT structure-derivation
  contract described in `references/dft-vasp-reference.md`.

If the user provides only a folder, use only supported files (`.xyz`, `.extxyz`,
`.cif`, `POSCAR`, `CONTCAR`, `.vasp`). Reject unsupported files and mixed structure
formats in one directory rather than silently changing frame order or parser
semantics. Record every source file path, format, frame count, and SHA-256.

Current example files are `selected_order_from_merged.xyz` and
`dft_reorganized.csv`; treat these as an example, not a global default.

## Inspect Structure File

Use ASE (`ase.io.read(path, index=":")`) for every supported structure input.
Preserve lattice and PBC metadata. Do not parse coordinates manually unless
ASE fails and the user approves a fallback. The normalized frame list and its
source provenance are the input inspection source of truth.

Determine:

- Frame count and atom count per frame.
- Whether each source is plain XYZ, extxyz, CIF, POSCAR/CONTCAR, or `.vasp` VASP format.
- The input mode (`single_file` or `directory_sorted_filename`) and source
  hashes.
- Whether frame order is stable and should be preserved.
- Adsorbate species, atom count, and selection rule.
- Slab composition per frame, excluding adsorbate atoms.
- Whether adsorbate atoms appear split across periodic boundaries.

Do not treat extxyz `energy`, `forces`, `calculator`, or similar metadata as the
DFT reference unless the user explicitly says so.

When `dft_reference_mode=generate_vasp`, keep the original source read-only.
The VASP preparation controller operates on copied ASE `Atoms` frames and
writes a standard VASP5 `POSCAR` separately for every slab+adsorbate, clean
slab, and isolated-adsorbate calculation. Record the source-file hashes, the
normalized structure hash, and the generated POSCAR hash in the input
manifest.

Validate adsorbate selection. For `last_N_atoms`, inspect several frames
including first and last frames and confirm selected atoms match the adsorbate
formula. If not, stop and ask.

## Boundary Handling

Adsorbates may cross cell boundaries. Define and record a boundary-handling rule
before calculations:

- Preserve original input files.
- Work on copied `Atoms` objects for wrapping/unwrapping.
- Reconstruct adsorbates into contiguous molecules for isolated adsorbate
  calculations when needed.
- Do not distort or strain cells to force success.
- Ask the user before applying a non-obvious repair rule.

Useful rules include ASE wrapping, minimum-image adsorbate unwrapping, rigid
translation of the adsorbate group, or tag/index-based selection.

## Define Relaxed-Structure Reliability Classification

When `relaxation` is requested, apply the source-agnostic protocol in
`references/relaxed-structure-classification.md` to both MLP and VASP
slab+adsorbate relaxed structures before Step 3. Record the fixed priority,
source type, invalid-data rules, monitored intramolecular bonds, surface atom
selection, distance/overlap/displacement thresholds, PBC/minimum-image
treatment, and human-readable definitions in
`relaxation.relaxed_structure_classification`.

The categories are mutually exclusive and must be evaluated in this order:
`Invalid → Unconverged → Dissociated → Detached → Intact and converged`.
For a NO3 configuration, select the configured N,O,O,O atoms, monitor the three
N-O minimum-image distances, use `1.8 Å` for dissociation, and use final
adsorbate–surface distance `>3.5 Å` plus increase `>0.6 Å` for detachment. These
values are a dataset configuration, not universal defaults for other adsorbates.

## Freeze Result-Validity Rules

Record `result_validation` in `benchmark_config.json` before running models:

```json
{
  "require_energy_components": true,
  "energy_identity_tolerance_eV": 1e-6,
  "require_relaxation_fmax": true,
  "force_tolerance_eV_per_A": 1e-8,
  "max_abs_adsorption_energy_eV": null
}
```

Use a dataset-approved positive value for `max_abs_adsorption_energy_eV` only
when a scientifically justified adsorption-energy bound exists. Do not invent a
universal range. Treat these fields as part of the frozen scientific definition.

## Freeze The Adsorbate Reference State

Define the isolated-adsorbate reference before any model is run. Record under
`adsorbate_reference_policy`:

- `scope = per_model`; never reuse an energy produced by a different model.
- Geometry source and whether the molecule is fixed or relaxed.
- Charge and spin multiplicity, including unsupported-setting behavior.
- PBC, cell vectors, centering, vacuum thickness, and `compute_stress` policy.
- Whether the same reference state applies to SP and Relax.
- Whether immutable loaded weights may be shared between calculator objects,
  with required isolation-test evidence.

Do not assume `pbc = false` is safe for every provider. Run a finite-energy probe
for the isolated molecule. Allow a provider-specific PBC/cell exception only
when recorded in `scientific_exceptions` and applied consistently.

## Freeze Relaxation Semantics

When Relax is requested, record optimizer, maximum steps, force criterion, cell
policy, bottom-layer selection, layer-clustering tolerance, trajectory policy,
and treatment of unconverged structures. Define a benchmark-wide baseline.
Freeze the baseline as FIRE, `fmax=0.05 eV/A`, fixed cell, and 1000 maximum
steps. Require an automatic 5000-step rerun for any frame still unconverged at
1000 steps. Permit BFGS only when the model/provider cannot support FIRE, and
record both the concrete incompatibility and a scientific-exception ID. Do not
use convergence speed or preference as a fallback reason.

Define the slab constraint boundary exactly: a slab with 5 layers or fewer must
fix its bottom 1 layer; a slab with 6 layers or more must fix its bottom 2
layers. Detect layers before applying constraints and record both detected and
fixed layer counts per Relax frame. Record model-specific exceptions instead of
silently changing optimizer, step limits, constraints, or cell treatment.

Confirm the Relax DFT target has compatible semantics. If it is a single-point
target at a different geometry, do not label the result a relaxed-energy
benchmark without explicit approval and a visible limitation.

## Inspect DFT Table

Skip this section only for `generate_vasp`. For `provided`, validate the table,
then run `scripts/dft_reference_control.py validate-provided <benchmark_root>
--owner-id <owner>` so the aligned table and provenance are frozen under the
run. Prefer a valid supplied table by default; recompute only when it is absent
or the user explicitly requests VASP recomputation.

The reference table only needs enough information to align rows with structures
and provide DFT adsorption energy. Do not require `order`, `frame`, `cluster`, or
`element`. Composition should normally be derived from the matched structure as
slab composition excluding adsorbate.

Column aliases are hints, not hard rules:

- Adsorbate energy: `ads`, `adsorbate`, `E_adsorbate`, or adsorbate formula.
- Slab energy: `slab`, `E_slab`.
- Slab-plus-adsorbate: `slab-ads`, `slab_ads`, `slab_plus_ads`,
  `E_slab_ads`.
- DFT target: `ads(DFT)`, `E_ads_DFT`, `adsorption_energy`,
  `adsorption_energy_eV`.

If aliases conflict or multiple target columns are plausible, ask the user.

## Resolve DFT Target

Valid cases:

- Final DFT adsorption-energy target column exists; use it directly.
- Decomposed energies exist; compute
  `E_ads_DFT = E_DFT(slab + adsorbate) - E_DFT(slab) - E_DFT(adsorbate)`.
- Both exist; compare them and ask if disagreement exceeds rounding tolerance.

Assume eV only when headers, project context, or user confirmation supports it.
Otherwise ask and record `energy_unit`.

## Align Frames To Rows

Prefer:

1. Explicit shared IDs if both files provide reliable IDs.
2. CSV row order matched to zero-based structure index.
3. User clarification if counts/order do not match.

Record `structure_index` as zero-based. Step 2 records `selection_index` as
one-based for both modes. It records `snapshot_rank` only for
`matbench_top_n`; the field is null for `user_specified`.

## Required Artifacts

Write `benchmark_config.json` and `input_inspection_report.json` under
`benchmark_root`.

Use `assets/benchmark_config.schema.json` as the field guide. The config must
include `benchmark_request` with selection mode, model count, dataset name,
the requested adsorbate, and requested tasks, plus paths,
frame count, adsorbate definition, boundary rule, DFT target, energy unit,
alignment rule, and `composition_definition =
slab_composition_excluding_adsorbate`.
For generated VASP references, set `dft_reference_path=null`, freeze
`dft_reference.structure_derivation`, and use the three-relaxation formula from
`references/dft-vasp-reference.md`.

Use `input_inspection_report.json` to record file type, frame/atom counts,
adsorbate validation, slab composition, boundary warnings, row count, DFT target,
energy unit, and alignment check result.

## Progress And Stop Conditions

Verify that the observed structure adsorbate matches the request frozen in
`run_manifest.json`. On success run `scripts/run_control.py initialize-frames`
with the verified frame count and observed adsorbate. This atomically initializes
every requested model/task checkpoint. On mismatch it blocks the run and never
renames the benchmark directory. Keep request identity unchanged.

Do not proceed to Step 2 until these are resolved:

- Structure file path.
- DFT reference path for `provided`, or confirmed VASP/execution-profile inputs
  and structure derivation for `generate_vasp`.
- Requested model count.
- Unambiguous model-selection mode and, for `user_specified`, complete identity
  confirmation for every requested model.
- Dataset name.
- Requested task selection.
- Frame count.
- Adsorbate species, atom count, and selection rule.
- Boundary-handling rule.
- Adsorbate dissociation rule when relaxation is requested.
- DFT target column or formula.
- Energy unit.
- Frame-to-row alignment rule.
- v4 frame checkpoints initialized with the frozen frame count.
- Complete adsorbate-reference policy.
- Task-compatible DFT target semantics.
- Complete relaxation protocol and exception policy when Relax is requested.

Freeze benchmark_config.json.structure_indices as the complete consecutive
zero-based input ordering before evaluating models. It must equal
list(range(frame_count)); never derive it from successful predictions or Relax
outcomes. Preserve source file order and hashes with this list.
