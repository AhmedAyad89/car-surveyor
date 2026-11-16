# Comparison: Haiku without thinking against Claude Code Opus, as the extractor

Run 2026-11-16. The 90 documents Claude Code Opus had read through `--export` and
`--import` (label `claude-code-opus`: 9 price sheets, 81 spec sheets) were read
again through the API by `claude-haiku-5-5` with thinking off, at effort `high`,
with the same prompts (`pricing-v1`, `specification-v1`). Each set of answers was
verified and resolved on its own copy of the database. No reference data was used.

Haiku's 90 calls cost $0.38: 1.64M input and 0.44M output tokens. None failed.

## Price sheets: Haiku matches Opus

| Measure | Result |
|---|---|
| Rows returned, against priced rows in the tables | the same on 8 of 9 sheets; one extra row on the ninth |
| MSRP on rows both returned | the same on 1,886 of 1,886 |
| Delivery fees and document dates | the same on every sheet |
| Description split into model, powertrain, grade, drive, engine, transmission, body | the same on 98% to 100%, by field |
| Style claims rejected by verification | 8, against Opus's 0: a model code printed twice used as the row label |
| Resolved MSRP and delivery fee, on styles both have | the same on every style |

## Spec sheets: Haiku's figures are mostly right and mostly not admitted

| Measure | Opus | Haiku |
|---|---|---|
| Values returned | 1,338 | 1,087 |
| Values rejected by verification | 16 | 756 |
| Opus values Haiku also returned (same attribute and number) | | 1,073, or 80% |

Why Haiku's values were rejected:

| Reason | Count |
|---|---|
| A table cell locator with an empty or unknown column header | 443 |
| The quote says "estimated" and the claim does not record it | 120 |
| A row label that is not in the table, often the label run into the cell text | 117 |
| The quote is not in the located cell | 61 |
| The value is not one number | 13 |

After resolve, horsepower was empty on 381 of the 625 styles where Opus filled it,
and torque on 379 of 609. Highway mpg was empty on 92 more styles, where the EPA file
does not cover them. On the values both models returned, Haiku scoped them
differently from Opus often: engine on 220, grades on 182, drive on 47.

## What follows

- **The API reads price sheets with Haiku**, without thinking. `just extract` now
  reads only price sheets unless `--kind` says otherwise.
- **Spec sheets stay with Claude Code agents running Opus**, through `--export` and
  `--import`, with the label `claude-code-opus`.
