# DFT Reference Branch: VASP Through DPDispatcher

Use this reference when `dft_reference_mode=generate_vasp`. The DFT branch and
MLP branch may run concurrently, but Step 4 is a merge barrier and may consume
only a ready DFT reference.

## Contents

1. Scientific contract
2. User-confirmed profiles
3. Structure derivation
4. POTCAR generation
5. DPDispatcher execution
6. Collection and recovery
7. Partial coverage

## Scientific Contract

Relax all three energy components with the same frozen VASP profile:

```text
E_ads_DFT = E_DFT(relaxed slab+adsorbate)
          - E_DFT(relaxed clean slab)
          - E_DFT(relaxed isolated adsorbate)
```

Use fixed cells. Apply the benchmark slab constraint rule to slab-containing
systems. Reconstruct the isolated adsorbate with minimum-image vectors, center
it in the user-confirmed periodic vacuum cell, and relax it with the same
electronic settings unless the user confirms a documented exception.

Never infer ENCUT, functional, dispersion, pseudopotential family, k-point
mesh, spin, smearing, dipole correction, convergence limits, VASP executable,
or remote resources from an old project. Require a complete explicit profile.

## User-Confirmed Profiles

Before preparation, follow `references/dft-defaults.md` and generate an
unconfirmed proposal with `scripts/dft_profile.py`. Display all component
settings, resolve system-specific fields, and obtain explicit user acceptance.
Only the confirmed result conforms to `assets/vasp_profile.schema.json`; its
confirmation hash must match the effective settings. Never auto-confirm a template.

Create `execution_profile.json` conforming to
`assets/dft_execution_profile.schema.json`. It must use `backend=dpdispatcher`
and `credentials_policy=external_only`. Authentication must come from an SSH
agent, SSH configuration, or environment-owned mechanism. Never store a
password, token, private key, or secret-valued field in the run.

Freeze the selected POTCAR backend in the VASP profile. A change of functional,
POTCAR backend/root/labels, INCAR, KPOINTS, relaxation criterion, or isolated
molecule cell creates a new profile hash and requires new DFT preparation.

## Structure Derivation

Step 1 must confirm zero-based adsorbate atom indices across representative
frames. The preparation controller then:

- preserves each supplied frame as the slab+adsorbate input;
- removes exactly those indices to derive the corresponding clean slab;
- extracts and minimum-image unwraps those indices to derive the adsorbate;
- determines bottom slab layers using the frozen layer tolerance;
- fixes one bottom layer for slabs with at most five layers and two for slabs
  with at least six layers;
- deduplicates calculations by structure plus VASP-profile hash.

Run preparation with an ASE-capable, explicitly recorded interpreter:

```text
python scripts/dft_reference_control.py prepare <benchmark_root> \
  --owner-id <owner> --vasp-profile <confirmed-profile.json> \
  --execution-profile <confirmed-execution-profile.json>
```

Preparation writes POSCAR, INCAR, KPOINTS, `POTCAR.spec`, immutable manifests,
per-calculation checkpoints, and the frame-to-calculation mapping. It never
writes licensed POTCAR content into returned benchmark artifacts.

## POTCAR Generation

Two remote backends are supported. The chosen backend is immutable for a
profile; never silently fall back from one to the other.

### VASPKIT backend

Use only a user-confirmed VASPKIT executable, version, task number, input lines,
functional family, and element-to-POTCAR labels. VASPKIT must be configured on
the remote VASP host with authorized pseudopotential access. The generated
element order must match POSCAR.

### Direct backend

Use a user-confirmed authorized remote `potcar_root` and explicit element label
mapping. Concatenate `<potcar_root>/<label>/POTCAR` in POSCAR element order.
Never download, return, or archive POTCAR itself.

For both backends, the remote command records only `POTCAR.titel` and
`POTCAR.sha256`. Collection rejects a result if TITEL order does not match the
frozen labels or if the SHA-256 record is invalid.

## DPDispatcher Execution

### Mandatory execution preflight

After `prepare` and before formal submission, collect a secret-free runtime
probe using the same remote shell, MPI launcher, VASP executable, Slurm
resource shape, and wrapper that the calculation will use:

```text
python scripts/dft_reference_control.py preflight <benchmark_root> \
  --owner-id <owner> --runtime-probe dft/runtime_probe.json
```

The probe must record submission-shell and job-shell `ulimit -s`, VASP and MPI
versions, available CPU/memory, scheduler evidence, and a passed representative
smoke task. For the confirmed Hermes/sari06 VASP 5.4.4 case, both shells must
report `unlimited`. This is an execution compatibility policy, not a universal
VASP scientific setting.

Generate and inspect the submission descriptor before a paid or long run:

```text
python scripts/dft_reference_control.py submit <benchmark_root> \
  --owner-id <owner> --dry-run
```

After user authorization, remove `--dry-run`. Submission is rejected unless
`dft/execution_preflight.json` is passed and still matches the execution
profile. DPDispatcher transports declared
inputs, builds POTCAR on the authorized remote host, runs the frozen VASP
command, and returns only declared nonlicensed outputs.

Record a job ID or PID for every active calculation and heartbeat the lease
every 60 seconds. On resume, query scheduler/accounting state and reattach
monitoring before resubmission. Lease takeover requires reconciling existing
DFT and MLP jobs.

DFT and MLP are independent compute branches. Submit under the single-writer
lease, then allow remote computations to remain active concurrently. Do not
wait for all DFT jobs before starting MLP jobs, and do not merge before the DFT
gate is ready.

While jobs are active, inspect output evidence without applying a time limit:

```text
python scripts/dft_reference_control.py monitor <benchmark_root> --owner-id <owner>
```

The monitor writes `errors/output_diagnostics.json` for every calculation.
Long runtime, no new output, an unfinished `vasprun.xml`, and a slow SCF step
never request termination. A termination request is written only after a
frozen diagnostic rule matches; the scheduler must then be reconciled before
collection.

## Collection And Recovery

Reconcile after querying the remote scheduler. Save the result as a secret-free
v4 scheduler-evidence JSON containing every calculation ID:

```text
python scripts/dft_reference_control.py reconcile <benchmark_root> \
  --owner-id <owner> --scheduler-evidence <scheduler-evidence.json>
```

Active jobs are reattached, terminal jobs are not treated as successful until
their output files pass collection, and unknown scheduler states block both
recovery and resubmission. Reconciliation evidence is hashed and stored under
`dft/reconciliation/`.

Collect after outputs or scheduler states change:

```text
python scripts/dft_reference_control.py collect <benchmark_root> --owner-id <owner>
```

Collection validates `vasprun.xml`, the configured final energy, electronic
convergence, and maximum force; requires positive electronic and ionic
convergence markers from OUTCAR; requires a final ionic-step energy record from
OSZICAR; and cross-checks the final energy against both files using the frozen
profile tolerance. POTCAR TITEL order and POTCAR SHA-256 are also required.
It atomically updates calculation checkpoints, DFT state, central run state,
event log, and `summary/dft_reference.csv`.

Verified output is the completion source of truth. A checkpoint without valid
output returns to recovery. Valid output written before a state update is
reconstructed by `collect`. Preserve failed output and retry counts.

Terminal failures are classified in
`calculations/<calculation_id>/errors/diagnostics.json`. Infrastructure failures
such as `soft_stack_sigsegv`, `mpi_launch_failure`, `scheduler_rejection`,
`resource_unavailable`, and `transport_failure` remain distinct from
`electronic_nonconvergence`, `ionic_nonconvergence`, and
`energy_crosscheck_failure`. Only same-profile wrapper or transport recovery
is automatic; scientific parameter changes require a new profile and user
decision.

## Partial Coverage

Full DFT coverage is the default merge gate. When bounded retries leave failed
calculations, remain blocked and show failed calculations and affected frames.
Partial metrics require a new explicit user decision:

```text
python scripts/dft_reference_control.py approve-partial <benchmark_root> \
  --owner-id <owner> --approved-by user --reason <documented-reason>
```

The merge then uses only frames with valid DFT energies, marks other frames as
missing due to `dft_reference`, retains the full expected-frame denominator,
and reports DFT coverage. Never hide failed VASP calculations.

Prepared manifests are immutable. Parameter changes invalidate existing inputs,
submission and collection evidence; create a new run for changed settings after
reconciliation of existing jobs. Do not delete active work or reuse old energies.
