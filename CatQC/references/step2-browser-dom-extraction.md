# Step 2 Browser DOM Extraction

Use this procedure whenever Step 2 reads the Matbench Discovery leaderboard.
The rendered visible table is the ranking authority.

## Non-Negotiable Method

Use a real browser session or Playwright with Chromium to render the page and
read the visible table DOM. Do not use `requests`, `curl`, downloaded page
source, an API response, repository data files, or cached leaderboard data to
decide the current top N. Those sources may be archived as supporting evidence,
but they cannot determine the frozen ranking.

Try these browser URLs in order:

1. `https://matbench-discovery.materialsproject.org/`
2. `https://janosh.github.io/matbench-discovery/`

Use the second URL when the first is blocked by anti-bot protection, remains
blank, or does not render the interactive table. Record both the canonical URL
and the effective rendered URL; store the latter as `source_url` in the
worklist. If neither renders the table, stop and ask the user for a new
browser-accessible route; do not fall back to a non-rendered data source to
infer the ranking.

## Configure And Verify The Visible View

Use the page's visible controls and verify their rendered state:

- Column preset is `Discovery`; confirm its active/selected state.
- Test set is `Unique Prototypes`.
- The visible rank column is `#`.
- CPS is ordered from higher to lower.
- Visible `#` values run upward from rank 1.

Treat the displayed numeric `#` as the final rank. The CPS arrow or label only
confirms the ranking direction; do not replace the displayed rank with a
locally recomputed CPS order.

Wait for the table itself, not merely page load. Use role, label, or visible-text
locators rather than brittle generated CSS classes when possible. If the table
uses pagination or virtualization, paginate or scroll until ranks 1 through N
have each been rendered and collected.

## Extract From The Rendered DOM

For every selected row, read the visible rank, model display name, metrics, and
links from the rendered table DOM. Key records by the displayed numeric rank,
not DOM position. Ignore hidden duplicate rows created by responsive or
virtualized layouts.

Save:

- Rendered HTML after the view is configured.
- Raw DOM-extracted rows before normalization.
- Normalized top-N CSV and JSON.
- Screenshot or screenshots that visibly cover all selected rows.
- Metadata with browser engine/version, canonical URL, effective URL, access
  times, view settings, extraction method, and sanity-check results.

Use `rendered_browser_dom` as the extraction method.

## Mandatory Sanity Check

Do not freeze the worklist until all checks pass:

1. Extract exactly N rows.
2. Confirm displayed ranks are the unique contiguous integers 1 through N.
3. Confirm every selected model name is nonempty.
4. Confirm displayed CPS is non-increasing by rank when numeric CPS is present.
5. Compare every extracted rank/name pair with screenshots from the same
   configured browser session and record `dom_screenshot_match_count = N`.
6. Record first and last rank/name pairs in snapshot metadata.

If any row differs between DOM and screenshot, recapture after rechecking the
view. Do not average, merge, or choose between conflicting lists.

For historical regression only, the screenshot-validated 2026-07-11 top-30
capture included these anchors:

- Rank 1: `TECE-OAM-RRA-1.0`
- Rank 2: `EquFlashV2`
- Rank 3: `EquiformerV3+DeNS-OAM`
- Rank 4: `GRACE-3L-OAM-L`
- Rank 30: `Allegro-MP-L`

These anchors diagnose reproduction of that dated capture only. Never require
them for a newer live snapshot because the leaderboard may change.

## Failure And Resume Rules

Do not mark Step 2 complete when browser evidence is missing, the screenshot
does not cover all N rows, or the DOM/screenshot comparison is incomplete.
Do not trust a pre-existing worklist unless its recorded snapshot evidence
satisfies these checks. Preserve an older inconsistent worklist as historical
evidence and create a new timestamped snapshot after successful extraction.
