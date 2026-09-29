# DFT defaults, preview and explicit selection

Use this workflow only for generate_vasp. A provided DFT reference table retains
its original parameters and does not trigger VASP. Defaults are a starting
proposal, not proof of convergence for every material.

| Setting | Default |
|---|---|
| Functional / potentials | PBE (GGA=PE) / PAW-PBE |
| ENCUT | 520 eV |
| EDIFF / NELM | 1E-5 / 200 |
| IBRION / ISIF / NSW / POTIM | 2 / 2 / 500 / 0.5 |
| EDIFFG | -0.05 eV/angstrom |
| PREC / LREAL / LASPH | Accurate / false / true |
| ISMEAR / SIGMA | 0 / 0.05 eV |
| Slab+adsorbate and clean slab | Gamma-centered 2x2x1, zero shift |
| Isolated adsorbate | Gamma-only 1x1x1 |
| ISPIN | 2; initial moments must be resolved per system |
| Dispersion / dipole correction | IVDW=0 / LDIPOL=false |
| Charge | Neutral for each component |
| Molecular vacuum | 10 angstrom on each side |
| Energy | energy_sigma_to_zero |

No default total spin is imposed. The null spin_multiplicity means no NUPDOWN
constraint, not singlet. Resolve initial_magmoms_by_element for all three
components from the actual system and show it to the user; atom-order MAGMOM
arrays are generated from that scheme. Resolve POTCAR dataset_version,
element_labels, backend and backend-specific paths/version evidence from the
actual authorized installation. Do not infer these or execution resources from
an older project. Obtain the execution profile using the existing preflight
workflow. Preserve fixed cells, fixed bottom layers and scientific exceptions.

```text
python scripts/dft_profile.py draft <proposal.json>
python scripts/dft_profile.py draft <resolved-proposal.json> --overrides <user-overrides.json> --selection-source "user request and verified current system"
python scripts/dft_profile.py preview <resolved-proposal.json>
```

Overrides recursively replace specified fields. selection_provenance preserves
the original defaults, overrides and their source. Draft files are unconfirmed;
show the complete preview, including effective settings for all three components
and unresolved fields. Ask whether to accept or customize. After any edit, show
the final preview again. Only after explicit user acceptance:

```text
python scripts/dft_profile.py confirm <resolved-proposal.json> --user-confirmed-preview-sha256 <digest-from-accepted-preview>
```

The confirmation binds the scientific settings and provenance. Resume with an
unchanged confirmed profile without asking again. A changed profile requires
new preview and confirmation; existing prepared manifests are immutable, so
changed settings require a new run after reconciling active calculations.

The confirmed schema is `assets/vasp_profile.schema.json`. The unconfirmed
template `assets/vasp_defaults.json` intentionally cannot pass preparation.

Keep duplicated settings consistent: EDIFFG must equal the negative force
threshold, NSW must equal max_ionic_steps, ISIF must be 2, and ISPIN must match
each component's ispin. A custom change must update both representations.
Component incar overrides are included in the preview; electronic-setting
exceptions across energy components require the existing scientific exception
record. Common kpoints apply to both slab components; the molecule is always
Gamma-only. An explicit molecular spin_multiplicity writes NUPDOWN=multiplicity-1.

For charged calculations, record verified valence_electrons_by_element and
valence_source for the chosen POTCAR dataset. NELECT is generated as the sum of
these ZVAL values in the actual component minus charge. Explicit NELECT must
match; missing or conflicting values block preparation. For neutral components,
omit NELECT unless explicitly needed and verified. Preparation writes effective
INCAR/KPOINTS and hashes them; submission checks those hashes before execution.

Validation:

```text
python scripts/dft_defaults_self_test.py
python scripts/dft_self_test.py --fixture-root <empty-test-directory>
```

For antiferromagnetic or site-dependent initialization, use initial_magmoms as
an explicit atom-order array for that component and set
initial_magmoms_by_element to null. The array length must match the actual
component. This preset supports collinear spin; noncollinear/SOC calculations
require a separately reviewed protocol rather than silently reinterpreting MAGMOM.

VASP tag semantics: [NELECT](https://vasp.at/wiki/NELECT),
[NUPDOWN](https://vasp.at/wiki/NUPDOWN), and
[MAGMOM](https://vasp.at/wiki/MAGMOM). Defaults above are the selected CatQC
proposal; these references explain tag meanings, not universal numerical convergence.
