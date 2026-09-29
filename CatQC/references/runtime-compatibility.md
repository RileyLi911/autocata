# Runtime Compatibility Evidence

Read this file only for the known sari06/Hermes deployment or after a matching
failure. These entries summarize observed project behavior, not evergreen
installation commands. Re-check official model instructions, current versions,
and the target node before applying a workaround.

## Record Before Applying A Workaround

Record the model, node, OS/glibc, compiler, Python executable, framework and CUDA
versions, exact error, official source, chosen workaround, affected files, diff
or SHA-256, validation command, and date. Never upgrade system glibc or patch an
environment that already produced validated results.

## Known sari06/Hermes Constraints

- Compute nodes have previously exposed glibc 2.28. Some newer PyG wheels require
  newer ABI symbols. Prefer compatible official wheels or source builds; do not
  upgrade system glibc.
- Docker and compute-node paths differ (`/workspace` versus
  `/home/hermes/workspace`). Resolve and record both paths before submission.
- Docker may not call `sbatch` directly; use the documented host submission
  channel when present.
- VASP 5.4.4 MPI jobs have produced `SIGSEGV` when a propagated soft stack
  limit was `8192 KB`. For the confirmed Hermes/sari06 environment, run the
  DFT execution preflight and require both the submission shell and the Slurm
  job shell to report `ulimit -s unlimited`. Record the before/after values and
  wrapper hash; do not change VASP scientific parameters as a response.
- The scheduler has rejected `--mem` and some GPU GRES requests. Probe accepted
  resource syntax before generating a full launcher.
- Network access may be disabled and NFS caches may be read-only. Redirect every
  provider cache into the model checkpoint directory. Obtain approval before
  enabling network access; disable it again when required by the platform.
- Clear inherited `PYTHONPATH` and `PYTHONHOME` in cluster jobs when they would
  override the recorded interpreter. Do not add environment activation logic if
  the user manages activation externally.

## Observed Model-Family Compatibility Cases

- Fairchem/OCP stacks have failed with SciPy versions that removed
  `scipy.special.sph_harm`; verify the official compatible range before pinning.
- PyTorch 2.6 changed `torch.load` defaults. Use `weights_only=False` only for a
  trusted, checksum-verified checkpoint and record the security decision.
- DPA 3.1 and DPA 4 have required incompatible DeepMD versions; keep separate
  environments and validate the interpreter path in every retry launcher.
- DPA-4 SeZM with `FixAtoms` has failed under BFGS; FIRE was a successful project
  exception. Record it as a protocol exception rather than changing all models.
- GRACE uses TensorFlow rather than PyTorch. Probe its GPU through TensorFlow and
  do not require a Torch import in generic job launchers.
- AlphaNet produced nonfinite isolated-molecule energies with `pbc = false` plus
  stress calculation. Probe the exact reference-state configuration and record
  any PBC/stress exception.
- Nequix has required compiler/backend and undeclared runtime dependencies.
  Validate imports and backend selection before downloading or launching fully.

## Script And Data Failure Regression List

Test these cases in smoke and final audits:

- `ok`, `success`, `error`, and `unconverged` status normalization.
- Actual measured `fmax`, not the threshold copied into the output field.
- Retry writes updated CSV rows and does not leave results only in stdout.
- `FixAtoms` or the configured constraint is present in every Relax script.
- extxyz writing removes unserializable calculator arrays without deleting
  scientific metadata.
- Frame identity uses the frozen positional `structure_index` mapping unless the
  config explicitly records another shared identifier.
- Container paths and symlinks resolve on the compute node.
