# Step 4: Merge Model Results And Compute Metrics

After all enabled models are complete, skipped, or blocked with user approval,
merge validated per-model outputs. Do not recompute predictions in this step.

## Reference Table

Treat Step 4 as a merge barrier. For `provided`, require state
`provided_ready`; for `generate_vasp`, require `complete` or explicit
`partial_approved`. Build and persist a clean DFT reference table from the
validated supplied table or the collected VASP branch:

```text
summary/dft_reference.csv

structure_index,slab_composition,dft_adsorption_energy_eV
```

Optional fields include `csv_serial`, `source_row`, `adsorbate_species`,
`n_atoms_total`, and `n_atoms_slab`. Preserve the alignment rule and slab
composition definition.

## Long Prediction Table

Run `scripts/validate_run.py <benchmark_root>` before merging, then use
`scripts/merge_benchmark_results.py <benchmark_root>`. Do not reimplement status
normalization, coverage checks, or metric formulas ad hoc.

Create:

```text
summary/benchmark_predictions_long.csv
```

One row per `task, model_key, structure_index`:

```text
task,model_key,model_name,selection_index,snapshot_rank,structure_index,slab_composition,dft_adsorption_energy_eV,model_adsorption_energy_eV,error_eV,abs_error_eV,converged,dissociation_status,reliability_category,invalid_reason,dissociation_reason,broken_bond_count,max_monitored_bond_distance_A,source_type,status,error_message
```

Use `task = SP` and/or `task = Relax` according to
`benchmark_request.requested_tasks`. Do not treat an unrequested task as missing
or failed.

Definitions:

```text
error_eV = model_adsorption_energy_eV - dft_adsorption_energy_eV
abs_error_eV = abs(error_eV)
```

For SP rows, set structure-classification fields to `not_applicable`. For Relax
rows, preserve the source-specific MLP/VASP reliability classification exactly.
The merge gate must also require fixed bottom-layer coordinate evidence:
`fixed_atom_indices`, `fixed_atom_max_displacement_A`,
`fixed_atom_displacement_tolerance_A`, and
`fixed_atom_displacement_violation=false`. Missing evidence or a displacement
above `0.05 Å` makes the Relax row invalid before metrics are computed.
Do not infer dissociation or detachment from energy error alone. Clean slab and
isolated adsorbate VASP calculations are energy-reference objects and are not
classified as adsorbate reliability structures.

## Dissociation Table

Create:

```text
summary/benchmark_dissociation_records.csv
```

Include one row for every Relax model/frame with model identity, structure
index, convergence, dissociation status/reason, monitored distances, broken
bond count, integrity method, and final POSCAR path. Keep `dissociated`
frames visible even when their energies are numeric.

## Metrics Table

Create:

```text
summary/benchmark_metrics_by_model.csv
```

One row per model and task:

```text
task,model_key,model_name,selection_index,snapshot_rank,n_expected,n_valid,n_failed,n_missing,coverage_fraction,n_intact,n_dissociated,n_integrity_unknown,mae_eV,rmse_eV,mean_error_eV,median_abs_error_eV,max_abs_error_eV,spearman,min_pred_eV,max_pred_eV
```

Compute metrics only on valid matched frames. Failed/missing frames are counted
but excluded from MAE/RMSE. The main Relax metrics include all numerically valid
frames, including marked dissociation; never remove dissociated frames silently.
For explicitly approved partial VASP coverage, use only common MLP/DFT-valid
frames, count absent DFT frames as `error_source=dft_reference`, retain the full
expected-frame denominator, and report coverage.

Determine numerical validity only through the shared `assess_result_row` helper.
It requires an eligible normalized source status, finite energy decomposition,
the configured adsorption-energy identity tolerance, consistent Relax
status/convergence, and the configured force threshold for successful Relax
rows. When the source says success but validation fails, emit effective
`status=failed`, retain `source_status=success`, and append the exact validation
reason. Do not silently repair or retain the row in metrics.
Treat optimizer fallback evidence, 1000-to-5000-step rerun evidence, fixed-cell
metadata, and slab fixed-layer counts as hard protocol invariants. Refuse merge
when any of these invariants is absent or inconsistent; these are not ordinary
per-frame numerical failures that may be summarized and ignored.

```text
MAE = mean(abs(model - DFT))
RMSE = sqrt(mean((model - DFT)^2))
mean_error = mean(model - DFT)
```

Compute Spearman only when at least two valid nonconstant paired values exist;
otherwise leave it blank. Record the metric schema version and do not silently
drop legacy columns.

If `n_valid = 0`, leave metrics blank/null and record reason.

Also create:

```text
summary/benchmark_metrics_relax_intact_only.csv
```

Compute this secondary table from valid Relax rows with
`dissociation_status = intact`. Record the number and exact indices excluded as
dissociated or integrity-unknown. Label this table clearly as secondary; it must
not replace the all-valid metrics table.

## Failure Table And Report

Create:

```text
summary/benchmark_missing_or_failed_frames.csv
summary/benchmark_merge_report.json
```

When Relax is requested, create the final structure delivery as one legal
POSCAR per valid converged frame:

```text
summary/structures/Relax/<model_key>/<structure_index>/POSCAR
summary/relaxed_structure_index.csv
```

The index records model/frame identity, convergence and dissociation status,
source frame manifest, source relaxed-structure SHA-256, POSCAR path and POSCAR
SHA-256. Convert the validated per-frame relaxed extxyz without rerunning
inference. Do not require an aggregate XYZ, and never concatenate multiple
POSCAR structures into one file.

Failure table columns:

```text
task,model_key,model_name,selection_index,snapshot_rank,structure_index,slab_composition,status,error_source,error_message
```

Include missing frames, failed rows, error CSV rows, and decomposition validation
failures.

Merge report should include row counts, valid counts, failure counts, warnings,
and a check that no metrics came from log-only data.

## Stop Conditions

Do not mark Step 4 complete until:

- Every enabled model appears in summary outputs.
- Every expected structure appears for each completed model/task as valid or
  failed/missing.
- Every Relax row has an integrity status, and every dissociated row appears in
  `benchmark_dissociation_records.csv`.
- Every valid Relax frame has exactly one readable POSCAR and one matching index
  record in `summary/relaxed_structure_index.csv`; failed or unconverged frames
  have no final POSCAR.
- All-valid and intact-only Relax metrics report their inclusion/exclusion
  counts explicitly.
- Every model's script/calculator isolation validation is present and passed.
- DFT order was preserved.
- `selection_index`, optional `snapshot_rank`, and `model_key` match the immutable
  `models_manifest.json`. User-specified models have blank/null `snapshot_rank`.
- A small sample of errors was recomputed manually or by validation code.
- all task checkpoints and `run_state.json` agree with the verified result rows.

## Report handoff

After merge validation and weighted ranking (or a recorded unavailable reason), pass these outputs to
`references/model-selection-report.md`. Write the final model status table and
reproducibility notes before extracting report evidence. Keep this report step
separate from prediction and preserve the existing metric definitions.
