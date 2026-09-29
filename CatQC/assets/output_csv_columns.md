# Output CSV Columns (Schema Version 2)

Use these field names unless the user explicitly requests a different schema.

## Per-Model Single-Point CSV

```text
structure_index,slab_composition,E_slab_plus_adsorbate_eV,E_slab_eV,E_adsorbate_eV,E_adsorption_eV,status,error_message,attempt_count
```

## Per-Model Relaxation CSV

```text
structure_index,slab_composition,E_slab_plus_adsorbate_eV,E_slab_eV,E_adsorbate_eV,E_adsorption_eV,converged,n_steps,fmax_eV_per_A,dissociation_status,dissociation_reason,broken_bond_count,max_monitored_bond_distance_A,monitored_bond_distances_A_json,integrity_check_method,reliability_category,invalid_reason,source_type,status,error_message,attempt_count,optimizer,optimizer_fallback_reason,max_steps,cell_fixed,slab_layer_count,fixed_layer_count,fixed_atom_indices,fixed_atom_max_displacement_A,fixed_atom_displacement_tolerance_A,fixed_atom_displacement_violation,scientific_exception_ids
```

## Error CSV

```text
structure_index,task,model_key,error_stage,error_type,error_message,traceback_path,retry_count,timestamp
```

## Benchmark Long Table

```text
task,model_key,model_name,selection_index,snapshot_rank,structure_index,slab_composition,dft_adsorption_energy_eV,model_adsorption_energy_eV,error_eV,abs_error_eV,converged,dissociation_status,reliability_category,invalid_reason,dissociation_reason,broken_bond_count,max_monitored_bond_distance_A,final_adsorbate_slab_distance_A,initial_adsorbate_slab_distance_A,adsorbate_slab_distance_increase_A,slab_max_displacement_A,min_pair_covalent_ratio,source_type,status,source_status,error_message,scientific_exception_ids
```

## Benchmark Metrics Table

```text
task,model_key,model_name,selection_index,snapshot_rank,n_expected,n_valid,n_failed,n_missing,coverage_fraction,n_intact,n_dissociated,n_integrity_unknown,mae_eV,rmse_eV,mean_error_eV,median_abs_error_eV,max_abs_error_eV,spearman,min_pred_eV,max_pred_eV
```

## Dissociation Records

```text
model_key,model_name,selection_index,snapshot_rank,structure_index,slab_composition,converged,dissociation_status,reliability_category,invalid_reason,dissociation_reason,broken_bond_count,max_monitored_bond_distance_A,final_adsorbate_slab_distance_A,initial_adsorbate_slab_distance_A,adsorbate_slab_distance_increase_A,slab_max_displacement_A,min_pair_covalent_ratio,monitored_bond_distances_A_json,integrity_check_method,source_type,poscar_path
```

## Relax Intact-Only Metrics

```text
model_key,model_name,selection_index,snapshot_rank,n_expected,n_valid_intact,n_excluded_dissociated,n_excluded_integrity_unknown,excluded_structure_indices,mae_eV,rmse_eV,mean_error_eV,median_abs_error_eV,max_abs_error_eV
```

## Missing Or Failed Frames Table

```text
task,model_key,model_name,selection_index,snapshot_rank,structure_index,slab_composition,status,source_status,error_source,error_message
```

## Final Model Status Table

```text
model_key,model_name,selection_index,snapshot_rank,enabled,environment_status,checkpoint_status,script_isolation_status,calculator_isolation_status,sp_status,relax_status,n_sp_valid,n_relax_valid,n_relax_intact,n_relax_dissociated,n_relax_integrity_unknown,final_status,notes
```

## Relaxed Structure Index

```text
model_key,structure_index,status,converged,source_frame_manifest,source_structure_sha256,poscar_path,poscar_sha256,dissociation_status,error_message
```

Each valid Relax frame has one standard POSCAR. Aggregate XYZ is not a required
final artifact, and POSCAR files must not be concatenated.

## Weighted model ranking

`summary/model_ranking.csv`:
```text
model_key,model_name,n_sp,sp_mae_eV,rho_sp,n_relax_common,relax_mae_eV,n_relax_expected,n_intact_converged,r_relax,score_relax_mae,score_sp_mae,score_spearman,contribution_relax_mae,contribution_sp_mae,contribution_spearman,total_score,rank
```
`summary/model_reliability_review.csv`:
```text
model_key,model_name,n_relax_expected,n_intact_converged,r_relax,invalid_count,unconverged_count,dissociated_count,detached_count,missing_count,unclassified_count,reliability_review_status,review_reason
```
The settings and audit are `summary/ranking_config.json` and `summary/ranking_audit.json`.
No numeric ranks are written when the audit is unavailable; the CSV has a header only.

## English report artifacts (non-CSV)

- `summary/model_selection_report.md`: agent-authored, evidence-linked English model selection report.
- `summary/model_selection_report_evidence.json`: source hashes, scope, all selected models, exact sample sets, metrics, reliability and verified weighted ranking evidence.
- `summary/model_selection_report_review.json`: report/evidence hashes and model-by-model and use-case semantic review attestation.

Other benchmark CSV contracts are unchanged.

Catalysis selection adds execution_variant, catalysis_assessment and leaderboard_identity to the model manifest and report evidence. Existing CSV columns and metric formulas are unchanged.
