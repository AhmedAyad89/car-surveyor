# Design

Detail behind the stages in [README.md](README.md#pipeline). This file
describes the pipeline as built. Where it differs from the plan of
2026-11-15, the difference is listed under
[Changes from the plan](#changes-from-the-plan).

## Stages

Every stage is idempotent. A second run makes no fetches and no model
calls, and leaves every table as it was.

- **`discover`** reads the pressroom's vehicle sitemap, the full-line
  pricing page and each vehicle page, and records every document they
  link to as a candidate, fetched or not. Sources are listed in
  `sources.yaml`, and the fetcher refuses any host that file does not
  name.
- **`snapshot`** fetches each candidate once and stores the raw bytes
  under their SHA-256, the text derived from them, and each table as a
  table. Toyota's PDFs sit on a host that refuses every client but a
  browser, so each comes from its Wayback capture, or from a file placed
  by hand in `data/inbox/`.
- **`extract`** parses `vehicles.csv`, and makes one model call per
  document with structured output. The answer is kept as returned, keyed
  on the snapshot hash and the extractor version, which names the model
  and the prompt. The quote and the locator are fields in the schema,
  because the citations feature cannot be combined with structured
  output.
- **`verify`** admits or rejects each claim against its document.
- **`resolve`** merges the same style seen in several documents,
  attaches each value to the styles it applies to, chooses a value per
  cell, and runs the consistency checks.

## Data model

One DuckDB file and a directory of snapshots named by hash.

```
candidates     url, source_id, kind, model_years, discovered_from,
               link_text, status, detail
snapshots      sha256, source_id, url, fetch_url, route, kind,
               media_type, raw_path, text_path, tables_path,
               fetched_at, capture_date, document_date
extractions    snapshot_sha256, extractor_version, status, response,
               detail, input_tokens, output_tokens, cost_usd
style_claims   claim_id, style_key, model_year, model, model_code,
               grade, powertrain, body, engine, transmission, drive,
               description, snapshot_sha256, locator, quote,
               extractor_version, status, reject_reason
value_claims   claim_id, style_key, applies_to, attribute, value_text,
               value_num, unit, qualifiers, hedge_flags,
               effective_date, source_class, snapshot_sha256, locator,
               quote, extractor_version, status, reject_reason
style_records  one row per style, rebuilt by resolve
style_values   one row per admitted claim attached to a style, with
               is_chosen and is_conflicting, rebuilt by resolve
check_results  check_name, style_key, attribute, detail
```

- **A style exists only if a document says so.** Styles come from the
  rows of pricing documents. A specification document or the fuel
  economy file never creates one.
- **`style_key`** is the model year and Toyota's model code where the
  document prints a code that no other row of it shares. A row with no
  code, or with a code the document prints twice, is keyed on what it
  says about itself: model, grade, powertrain, body, engine,
  transmission and drive. Transmission is part of that, because manual
  and automatic versions are priced separately.
- **A value claim names one style or a scope.** A price row names its
  style. A horsepower figure given for four grades, a delivery fee given
  for a class of models and a fuel economy row named `Camry AWD LE/SE`
  each keep what their document says they apply to, in `applies_to`.
- **Three dates are kept apart:** `document_date` (stated in the
  document, and admitted only if code finds the statement),
  `capture_date` (the archive's timestamp), and `fetched_at`. A value's
  `effective_date` is the date the document gives for it, or else the
  document date.
- **`value_text` is the value as printed;** `value_num` is the parsed
  number.
- **Qualifiers and hedges are stored.** For MSRP the qualifier says
  whether the delivery fee is `included`, `excluded` or `unstated`.

Views, in `sql/views.sql`: `styles` (one row per style with the chosen
value, its claim and its date per column), `disagreements` (cells where
the leading claims state different values), and `coverage` (per model
year and attribute: filled, left open, unstated).

## The evidence check

A locator is one of three kinds:

| Kind | Fields | Used for |
|---|---|---|
| text span | character start and end in the snapshot's text | prose |
| table cell | page, table, row label, column header | price and specification tables |
| file row | row key, column | `vehicles.csv` |

`evidence.py` says where a quote is. It compares parsed numbers, so
`495` does not pass inside `$28,495`. It returns the span a prose quote
was found at. It looks a table cell up by its row label and column
header, and a label that fits two rows names neither.

`verification.py` decides whether that place is the right one:

- **A style** must be a table row. Its description must be the located
  cell, its model code and model year must be in the row, and every word
  of its model, grade and other parts must be printed in the
  description.
- **A price** must sit under a header that names a price, be a dollar
  amount, and share its row with a style that was admitted.
- **A delivery fee** must be a dollar amount in its quote, with its
  vehicle class and every listed model printed after it.
- **A specification figure** must be the number its quote gives in the
  claim's unit (`203 hp @ 6,600 rpm` gives 203 in hp, not 6,600), or the
  city, highway or combined member of its triple. A hedge printed with
  it must be recorded on the claim.
- **A fuel economy figure** must be the attribute's own column of a row
  of the right make and model year.

Tables are read from the page geometry, not from the cells a PDF draws.
Toyota's specification tables rule their columns in the header row only,
and a table continued on a later page has no header. Each word is placed
by the column its centre falls in, and a continued table takes the
column boundaries of the table it continues.

Locations are computed on text derived from the raw bytes, never on a
library's cleaned output.

## Resolution

A scoped claim is attached to a style under three rules.

1. **A claim must not contradict the style.** A figure stated for AWD is
   not attached to a front-drive style. What the style does not print is
   unknown, and unknown does not contradict.
2. **The most specific claim wins.** A figure that names the style's
   grade beats one given for a whole column. A figure for the style's
   drive beats one that names its grade more exactly but no drive.
3. **Agreement decides, disagreement stays open.** If the leading claims
   for a cell state one value, that is the cell. If they state several,
   the cell is empty and listed in `disagreements`, unless another
   source states exactly one of them.

Three things are inferred from the rows printed beside a style, and only
when such a row exists: `Camry LE` beside `Camry LE AWD` is not
all-wheel drive, `3.0 Base` beside `3.0 Base MT` is the automatic, and
`SR Access Cab 6AT` beside `SR Access Cab V6 6AT` is not a V6. The drive
and transmission so inferred are marked as inferred.

Per attribute:

- **MSRP and delivery fee** are dated. The latest stands and earlier
  values are kept as history. A fee applies only to styles priced in the
  same document, and is matched through the document's own list of
  models.
- **Horsepower and torque** come from the Product Information document
  whose title covers the style's line. A hybrid shows its system output
  where the document gives one. Electric motor figures, and figures in
  kilowatts or newton metres, are admitted as claims and not attached.
- **Fuel economy** comes from the EPA file where its rows for a style
  agree, and from Toyota's document otherwise. A row is attached only if
  every word of its name is accounted for by the style.

## Consistency checks

Checks never read the reference, and a finding changes no value.

- **`drive_price_order`:** a four-wheel-drive style priced below the
  two-wheel-drive style that is otherwise the same.
- **`year_over_year_price`:** a model code whose price moved by more
  than 15% up or 5% down between two model years.
- **`fuel_economy_agreement`:** a fuel economy figure Toyota's document
  states that differs from the EPA file's.
- **`repeated_model_code`:** one model code on rows a document describes
  differently.
- **`repeated_description`:** one description priced twice in a
  document, at different prices.

Each check has a seeded-error test, and so does each verification rule:
a swapped digit, a price from the row below, two grade names exchanged,
a hedge removed. Every tightened pattern has a test for what it must
still match.

No document read so far prints a total beside a price and a fee, so
there is no arithmetic check. One belongs in `consistency.py` when a
document does.

## Viewer

A local page over the DuckDB file, for development: the catalogue, a
cell with its highlighted quote and every other claim, the documents and
what became of them, and the diff's bucket counts from the feedback
file. It shows a reference value only when the user starts it with the
detailed diff output, which no agent session can read.

## Changes from the plan

- **`powertrain` is a style column.** The pricing documents put Hybrid
  before the grade for some models and after it for others, so it is
  kept apart from the model and the grade.
- **`applies_to` on value claims, and `style_values`.** The plan had
  `style_key` on every value claim. A figure that applies to several
  styles needs a scope, and a table that says which styles it was
  attached to.
- **Consistency checks run in `resolve`, not `verify`.** They compare
  styles, which exist only after resolution.
- **The year-over-year band and the arithmetic check** are as described
  above: the first is implemented, the second waits for a document that
  prints a total.
- **The dashboard is a local viewer, not an Evidence site.** The user
  asked for a way to interact with the data and the diff during
  development.

## Wrinkles still open

- **Mid-year repricing.** Every price is a dated claim, and the latest
  stands. Only one edition per pricing document address is in the
  archive, so little history has been seen.
- **Hybrid torque.** Toyota's documents give the engine's torque and no
  system figure. The style shows the engine's.
- **Plug-in hybrids.** The EPA file's first-fuel figure is attached. Its
  electric-mode MPGe is not read.
- **Grade ranges.** A scope such as `LE → PLTM` is not expanded, so its
  figure is attached to every grade and is outranked by any figure that
  names a grade.
