# Source-independent relaxed-structure reliability classification

This protocol applies to both MLP Relax and VASP DFT Relax slab+adsorbate structures.
Categories are mutually exclusive, with fixed priority:

```text
Invalid -> Unconverged -> Dissociated -> Detached -> Intact and converged
```
Once an earlier condition is met, do not evaluate later categories for assignment.

## Normalized record

The adapter `scripts/classify_relaxed_frame.py` supplies the normalized record
required by `scripts/relaxed_structure_classification.py`: source_type, status,
converged or ionic_converged, energy_eV, initial/final coordinates, element order,
cell, and pbc. The policy supplies adsorbate_indices, surface_indices,
monitored_bonds, and geometric thresholds.

## Categories

- `Invalid`: missing structure/data, changed atom count or element order,
  invalid energy, unknown status, severe atomic overlap, or a surface displacement
  above the configured threshold after subtracting overall rigid translation.
- `Unconverged`: the structure is valid, but MLP converged/status or VASP
  ionic_converged/status does not establish convergence.
- `Dissociated`: valid and converged, but any monitored bond exceeds
  dissociation_bond_distance_A.
- `Detached`: not dissociated, and both the final minimum adsorbate-surface MIC
  distance exceeds detached_final_contact_distance_A and its increase relative
  to the initial structure exceeds detached_contact_increase_A.
- `Intact and converged`: none of the preceding conditions applies.

Distances use the configured periodic boundary conditions and minimum-image
convention. NO3-specific N-O bonds and thresholds must be supplied by the dataset
configuration, never hard-coded into the general classifier.

## Fixed bottom-layer displacement gate

In addition to checking fixed_layer_count metadata, every MLP and VASP Relax
record must compare the initial and final coordinates of the automatically
selected fixed bottom-layer atoms. A slab with at most five layers fixes its
bottom one layer; a slab with at least six layers fixes its bottom two layers.
The MIC displacement of every fixed atom must be at most
fixed_atom_max_displacement_A = 0.05 angstrom. Missing coordinates, fixed-atom
indices, or displacement evidence, or any displacement above the tolerance,
makes the record `Invalid`. Retain the selected indices, maximum displacement,
tolerance, and violation flag in the output.
