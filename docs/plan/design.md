# Design

Detail behind the stages in [README.md](README.md#pipeline).

## Stages

Every stage is idempotent. Output is keyed on the snapshot hash and the
extractor version, so a second run makes no fetches and no model calls.

- **`discover`** crawls the pressroom's listings and records every
  candidate URL and whether it was fetched. Sources are listed in
  `sources.yaml`.
- **`snapshot`** stores the raw bytes under their SHA-256, the text
  derived from them, and each table as a table.
- **`extract`** parses `vehicles.csv`, and makes one Claude API call per
  document with structured output. The quote and the locator are fields
  in the schema, because the citations feature cannot be combined with
  structured output. `extractor_version` records the model ID and the
  prompt version.
- **`verify`** runs the evidence check, then the consistency checks.
- **`resolve`** merges the same style seen in several documents and
  builds the views.

## Data model

One DuckDB file and a directory of snapshots named by hash.

```
snapshots     sha256, source_id, url, media_type, raw_path, text_path,
              fetched_at, capture_date, document_date
style_claims  style_key, model_year, model, model_code, grade, body,
              engine, transmission, drive,
              snapshot_sha256, locator, quote, extractor_version, status
value_claims  claim_id, style_key, attribute, value_text, value_num,
              unit, qualifiers, hedge_flags, effective_date,
              source_class, snapshot_sha256, locator, quote,
              extractor_version, status, reject_reason
runs          run_id, started_at, git_sha, split, metrics, tokens, cost
```

- **A style exists only if a document says so.**
- **`style_key`** is built from the model year, the model, and Toyota's
  model code where the document prints one. Transmission is part of the
  key, because manual and automatic versions are priced separately.
- **Three dates are kept apart:** `document_date` (stated or published),
  `capture_date` (the archive's timestamp), and `fetched_at`. A value's
  `effective_date` is the date the document gives for it, or else the
  document date.
- **`value_text` is the value as printed;** `value_num` is the parsed
  number.
- **Qualifiers and hedges are stored.** For MSRP the qualifier says
  whether the delivery fee is `included`, `excluded` or `unstated`.

Views: `styles` (one row per style with the chosen value, its claim and
its date per column), `disagreements` (cells where admitted claims
differ), and `coverage`.

## The evidence check

A locator is one of three kinds:

| Kind | Fields | Used for |
|---|---|---|
| text span | character start and end in the snapshot's text | prose |
| table cell | page, table, row label, column header | price tables |
| file row | row key | `vehicles.csv` |

Start from the old repository's `evidence.py` and fix three things:

1. **Compare parsed numbers, not substrings.** Today "495" passes inside
   "$28,495".
2. **Return where the quote was found.** Today the check returns only
   pass or fail.
3. **Add the table-cell kind.** A quoted bare number proves the number
   is in the document, not that it belongs to that row. The row label
   and column header must match the style and the attribute.

Compute locations on text derived from the raw bytes, never on a
library's cleaned output.

## Consistency checks

Checks never read the reference.

1. **Arithmetic the document contains:** a base price, a fee and a total
   must add up.
2. **SQL invariants:** all-wheel drive costs at least as much as
   front-wheel drive on the same grade; a style's year-over-year price
   change stays inside a band; stated mpg matches fueleconomy.gov.
3. **Agreement across sources** that state the same cell.

Each check has a seeded-error test: a swapped digit, a shifted table
row, two grade names exchanged, a hedge removed. Every tightened pattern
gets a test for what it must still match.

## Dashboard

Evidence, a static site built from SQL and Markdown over the DuckDB
file. Three pages: the catalogue (S1), the cell with its highlighted
quote and every other claim (S2), and the diff bucket counts (S3). It
never shows a reference value.

## Wrinkles to settle in S2

- **Mid-year repricing.** Every price is a dated claim.
- **Hybrids and electric vehicles** state system horsepower and MPGe.
  Store the unit and what the figure describes as printed. Never
  convert.
- **The delivery fee** differs by vehicle class and changes over time.
  It is its own dated value.
