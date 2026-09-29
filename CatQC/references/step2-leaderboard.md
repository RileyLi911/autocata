# Step 2: Resolve Model Resources

Model identity was frozen before Step 0 and is immutable in
`models_manifest.json`. Branch on `model_selection_mode` only to locate and
verify resources; never decide or replace identities here.

- `matbench_top_n`: use the rendered leaderboard workflow below. Its snapshot
  fixes model names, ranks, metrics, and original links.
- `user_specified`: do not open or fabricate a Matbench snapshot. Import the
  confirmed ordered mappings from `references/request-model-selection.md`, set
  `snapshot_rank = null`, and preserve `selection_index = 1..N`.

Neither mode requires blindly using broken links later; repair links through
official sources while preserving the frozen model identity. Any change of
model or variant requires explicit user reconfirmation.

## User-Specified Model Confirmation Evidence

For `user_specified`, read the confirmations from `models_manifest.json` and
preserve their authoritative evidence in `selection_evidence/`.

The array order, `selection_index`, and model array must agree exactly. A search
result without user confirmation is not sufficient. If a model could not be
found, remain in preflight and ask the user for an official URL, repository,
paper/DOI, package/provider, organization, checkpoint path, spelling, version,
or variant. Do not create a placeholder model or continue with fewer models.

## Required Leaderboard View

This section applies only to `matbench_top_n`.

Use:

- Canonical website: `https://matbench-discovery.materialsproject.org/`
- Browser-renderable fallback: `https://janosh.github.io/matbench-discovery/`
- Column preset: `Discovery`
- Test set: `Unique Prototypes`
- Rank source: displayed `#` ranking
- Primary rank metric: `CPS`, higher is better (`CPS up`)
- Selected models: ranks 1 through `benchmark_request.model_count` in that view

Do not switch to `Full Test Set`, `10k Most Stable`, `Phonons`, `Geo Opt`, `MD`,
or `Diatomics` unless the user explicitly changes the benchmark.

If the requested N exceeds the number of eligible rows available in the
required view, report the available count and ask the user how to proceed. Do
not silently reduce N or fill the worklist from a different leaderboard view.

## Mandatory Browser DOM Extraction

This section applies only to `matbench_top_n`.

Read and follow `references/step2-browser-dom-extraction.md` completely before
opening or extracting the leaderboard. Use a real browser or Playwright to read
the rendered visible table DOM. Never use `requests`, `curl`, page source, API
responses, repository data, or cached data to decide top N.

Record local and UTC access timestamps, browser engine/version, canonical URL,
and effective rendered URL. If neither documented browser URL renders the
interactive table, ask the user for a new browser-accessible route. Never invent
or infer the leaderboard from memory or a non-rendered data source.

## Snapshot Files

This section applies only to `matbench_top_n`.

Create a new timestamped directory each time:

```text
selection_evidence/matbench-YYYYMMDD-HHMMSS/
|-- matbench_leaderboard_rendered.html
|-- matbench_leaderboard_dom_rows.json
|-- matbench_leaderboard_screenshot.png
|-- matbench_leaderboard_top{N}.csv
|-- matbench_leaderboard_top{N}.json
`-- matbench_leaderboard_snapshot_meta.json
```

Never overwrite an older snapshot from an unfinished benchmark.
Replace `{N}` with the requested positive integer and record it as
`model_count` in snapshot metadata.

When pagination, scrolling, or virtualization prevents one screenshot from
covering all N rows, save numbered screenshots in the same directory and list
all of them in snapshot metadata.

For each selected row, store:

- `snapshot_rank` (1-based)
- `model_key` (stable normalized key)
- `model_name`
- displayed metrics: CPS, Acc, F1, DAF, Prec, MAE, R2, kSRME, RMSD
- Targets, Date Added, Links, Training Set, Org
- leaderboard row URL/anchor when available

## Resolve Resources

For each model, use the confirmed authoritative link first for
`user_specified`, or official leaderboard links first for `matbench_top_n`.
Then use official model cards, papers, package docs, GitHub repos, HuggingFace,
figshare, Zenodo, or Materials Project pages as needed.

Record:

- Source-code repository.
- Environment repository or instructions.
- Commit SHA, release tag, or package versions when available.
- Checkpoint/model-weight URLs.
- Planned checkpoint destination:
  `models/<model_key>/checkpoints/` relative to `benchmark_root`.
- Model card.
- License and usage restrictions.
- Auth/gated status.

For every model, also collect and preserve the official environment guidance
before any environment mutation. Prefer, in order, the exact repository's
`README`/environment files, official model-card installation instructions,
official package documentation, and official release notes. Save the retrieved
HTML/Markdown/environment file under:

```text
models/<model_key>/resources/official_guides/
```

Record the source URL, repository revision or document version, retrieval time,
SHA-256, authority, and which commands/packages/versions were used. Preserve
installation commands verbatim; any normalized command must be stored as a
separate derived field. If official instructions disagree, keep every source,
record the conflict, and stop before environment creation until the user
chooses the applicable release/variant.

Use a `resources` list because one model may need multiple repos, model cards,
checkpoints, or documentation pages. Distinguish papers, model cards, docs,
repositories, packages, and checkpoint files.

Step 2 discovers checkpoint sources and tests access only. Do not perform the
full checkpoint download in Step 2. Record the source, expected filename,
version/revision, expected size/checksum when available, and fixed Step 3
destination in the frozen worklist.

## Lightweight Access Tests

Before full downloads, perform lightweight checks:

- HTTP HEAD.
- Metadata request.
- Small ranged request.
- Provider CLI dry run or file listing.

Record status as `ok`, `gated`, `missing`, `broken`, or `unknown`, with method,
HTTP/error text, provider, file size, ETag/provider file ID, published checksum,
and retry count. Do not mark broken after a single timeout.

`ok` means access appears possible; full downloads later must still verify file
size and checksum when available.

## Gated Or Private Models

If access is gated/private, stop setup for that model and ask the user. Accept:

- Confirmation that the user authenticated through the provider CLI/session.
- Confirmation that the required secret environment variable is configured.
- Manually downloaded checkpoint path.
- Permission to skip.
- Explicit replacement model.

Never ask the user to paste a raw token into chat or a command that exposes it in
terminal output. Never store raw tokens in snapshots, configs, logs, progress
memory, or terminal captures. Redact accidental secrets.

Before a full download, record expected size, destination, available disk space,
license/terms, network route, and whether external state must change. Ask for
authorization when a license must be accepted, network access must be enabled,
or the download exceeds the configured approval threshold.

A user-provided checkpoint path is a source, not the benchmark runtime path.
Record it in the worklist as `source_kind = user_provided`; Step 3 must copy and
verify its required file or directory contents under the corresponding model's
fixed checkpoint directory before use.

## Repair Broken Frozen Links

If a frozen repo/checkpoint/doc link fails later:

1. Keep original `selection_index`, `snapshot_rank`, and `model_name`.
2. Revisit the Matbench leaderboard for `matbench_top_n`, or the confirmed
   identity source for `user_specified`, plus official linked sources for the
   same model.
3. Search only official or author-maintained sources unless the user approves
   unofficial sources.
4. Record corrected resource URL, access status, and repair date in the model's
   mutable resource-preparation artifacts; never edit `models_manifest.json`.
5. Preserve the original broken URL in `resource_repair_history`.
6. Append a resource-repair event and update `run_state.json`.

If the same model cannot be traced to an official working source, ask whether to
provide a local file, use unofficial source, skip, or mark a replacement.
Replacements must be explicit and must not be treated as original requested
models.

## Resource Artifacts And Stop Conditions

Write repository, license, environment, checkpoint-source, access-test, and
resource-repair records below each model directory. Keep gated models visible
in `run_state.json` as blocked rather than deleting them.

Do not proceed to Step 3 until:

- The immutable models manifest selection mode, order, and N match the request.
- For `matbench_top_n`: leaderboard access timestamp, frozen top-N snapshot,
  required view settings, rendered HTML, raw DOM rows, screenshots, browser
  version, URLs, contiguous ranks, and N-of-N DOM/screenshot matches exist.
- For `user_specified`: N authoritative direct links and N explicit user
  confirmations exist, ordered model identities match the request, and all
  `snapshot_rank` values are null.
- Every model has resource, repository, license, checkpoint-source, and access
  records or a visible blocking reason.
- Every model has saved and hashed official environment guidance, including
  the exact repository revision or document version used for setup.
- Gated/private decisions have user guidance.
- `run_state.json` and `events.jsonl` are updated by the lease owner.

## Confirmed catalytic configuration

Verify resources against both the preserved leaderboard identity and the actual
execution_variant. The latter was confirmed before Step 0 using
`references/catalysis-aware-selection.md`. Repair URLs without substituting a
checkpoint, task selector or head. Newly discovered incompatible or ambiguous
usage requires renewed selection, not a silent Step 2 change.
