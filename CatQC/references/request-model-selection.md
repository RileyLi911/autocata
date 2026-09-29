# Preflight: Freeze The Requested Models

Complete this preflight before Step 0 for both selection modes. First apply
`references/catalysis-aware-selection.md` to inspect exact-version official
GitHub sources, compare catalytic configurations and freeze the user-confirmed
execution_variant. This is part of the same model identity confirmation. Step 0 accepts
only a frozen selection payload; Step 2 may resolve runtime resources but cannot
change a model identity or version.

## Named Models

Preserve the user's ordered names. Search each exact name independently and
prefer an official model card, author repository, package documentation,
provider page, checkpoint page, or primary paper. Present the proposed exact
identity, version/variant, and at least one direct authoritative URL for every
model. Ask the user to confirm the complete numbered mapping.

If multiple credible variants exist, present them and ask the user to choose. If
no credible candidate exists, do not guess or drop the model: ask for an official
URL, repository, paper/DOI, provider/package, checkpoint path, exact spelling,
version, or variant. Do not create Step 0 state until every entry is confirmed.

## Matbench Top N

Require an explicit positive N. Read the Matbench Discovery leaderboard using
the browser procedure, freeze exactly ranks 1..N, and retain the browser evidence
files with the selection payload. Explain that the ranking is a model-selection
proxy, not an adsorption ranking. Resolve ambiguous releases before Step 0.

## Selection Payload

Write a JSON payload for `scripts/run_control.py --selection-file`. It contains the baseline identity fields below PLUS the required
`catalysis_assessment`, `execution_variant` and `leaderboard_identity` defined in
`assets/catalysis_selection.schema.json`. The abbreviated example below is not
a complete executable payload until those fields are populated:

```json
{
  "schema_version": 4,
  "model_selection_mode": "user_specified",
  "models": [
    {
      "requested_name": "MACE-MP-0",
      "confirmed_name": "MACE-MP-0 medium",
      "version_or_variant": "2024-01-07",
      "identity_url": "https://authoritative.example/model",
      "source_kind": "model_card",
      "confirmed": true,
      "confirmed_by": "user",
      "confirmed_at": "ISO-8601 timestamp",
      "snapshot_rank": null,
      "source_evidence": [{"url": "https://authoritative.example/model", "kind": "model_card"}]
    }
  ],
  "evidence_files": ["absolute/path/to/browser-evidence.json"]
}
```

For Matbench, use `model_selection_mode = matbench_top_n`, set contiguous
`snapshot_rank` values 1..N, and preserve rank order. For named models,
`snapshot_rank` is null and list order is authoritative. Step 0 assigns stable
`selection_index`, `model_key`, and per-model identity hashes and copies the
payload/evidence into `selection_evidence/`.

Stop if the mode is ambiguous, any named model lacks a confirmed authoritative
identity, a Matbench rank is missing/duplicated, or a variant remains unresolved.
