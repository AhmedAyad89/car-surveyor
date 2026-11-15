# Evaluation

How the pipeline is scored against OpenSpec without any agent seeing
OpenSpec. Read [isolation.md](isolation.md) first.

## Splits

| Split | Contents | Feedback reaches code? |
|---|---|---|
| Development | Toyota, model year 2023 | yes, under the feedback contract |
| Held out | the other five model years | no; run and scored once, in S4 |

## The diff script

`diff.py` is deterministic and makes no model calls. The user runs it
from their own terminal with `just diff dev`.

- **Reads** the resolved styles and `~/refdata/openspec/`, through
  `reference.py`.
- **Writes** the detail file, with both sides' values, to
  `~/refdata/out/<run_id>.json`, and the feedback file to
  `data/feedback/<run_id>.json`.
- **Writes no feedback file for the held-out split.** `just diff heldout`
  writes the detail file only, so nothing from those years can reach
  code before S4.
- **Matches** MSRP and the delivery fee when the reference value equals
  any dated pipeline value for the style, horsepower and torque within
  5, mpg within 1. A figure the pipeline holds in MPGe is compared with
  the reference's MPGe field, never with its mpg field.
- **Is tested** to prove the feedback file holds nothing the feedback
  contract forbids: it is a closed schema, and a test fills a reference
  with values that appear nowhere else and searches the file for them.
- **Refuses to start in an agent session.** The sandbox keeps an agent
  out of `~/refdata/`. The command also checks for the variable Claude
  Code sets in every agent shell, for a session started without the
  sandbox.
- **Names the file and the field in an error, never the value.**

`just reference-coverage` prints counts of the reference alone, per
model year: files, trims, configurations, and how many hold a model code
and each compared value.

The viewer shows the feedback file to anyone. Started with
`just view --diff-detail ~/refdata/out/<run_id>.json` it also shows the
reference's values, which only the user can do.

## What is compared

| Pipeline | Reference (`docs/reference_schema/`) |
|---|---|
| style | one configuration of one trim, in a model-year file |
| model code | `code`, `alternate_codes` |
| MSRP | `msrp` on the configuration |
| delivery fee | `destination_charge`, on the configuration or the model |
| horsepower | `powertrain.horsepower` |
| torque | `powertrain.torque_lb_ft` |
| mpg city, highway, combined | `fuel_economy.mpg_*`, or `mpge_*` for MPGe |

The reference inherits an attribute from the model, through a trim's
`extends` chain and the trim, to the configuration. The loader resolves
that before anything is compared.

Two definitions differ, and the diff does not hide either:

- **Torque of a hybrid.** The reference's field is system torque.
  Toyota's documents give the engine's and no system figure, so the
  pipeline holds the engine's.
- **A mid-year file.** A model year can have a second file, for a
  mid-year revision. A style is compared with both, and each is a
  reference row.

## Row matching

A reference row is one configuration. It is matched to a pipeline style
in two passes.

1. **By model code.** Same model year, and the style's model code is the
   configuration's `code` or one of its `alternate_codes`. Where the
   pipeline has two styles with that code, because a document printed it
   on two rows, the one whose description fits the row better is taken.
2. **By description**, for a reference row no code matched. The row and
   the style must be of the same model year and model, and must not
   contradict each other on drive, transmission, hybrid or plug-in,
   cylinder count or cab. The words of the trim name and cab are then
   compared with the words of the style's grade and body, and the pair
   is accepted when at least half of the words are shared. Pairs are
   taken best first, and ties are broken by key, so the result does not
   depend on the order of the input.

A style is used at most once per reference file, so a model year's file
and its mid-year file can both match it.

The feedback file reports how many rows matched by code, how many by
description, and how many did not match. A matching error looks like a
missed style, so its rate has to be visible. Only the user can inspect
individual matches, because that means seeing both sides: the detail
file lists each match, how it was made, its score, and whether the two
sides carry different codes. Agents tune the rule from the counts.

## Buckets

| Pipeline and reference | Primary document says | Bucket |
|---|---|---|
| agree | | `confirmed` |
| disagree | the pipeline's value | `reference_error` |
| disagree | the reference's value | `pipeline_error` |
| disagree | both, at different dates | `real_difference` |
| disagree | neither | `both_wrong` |
| disagree | nothing yet | `open` |
| reference empty | | `gap_filled` |
| pipeline empty | | `pipeline_miss` |
| style only in the pipeline | | `extra_style` |
| style only in the reference | | `missed_style` |

A cell the pipeline left open because its own sources disagree counts as
pipeline empty.

## Adjudication

The adjudicator never sees a reference value.

1. The feedback file lists the cells flagged `disagree` or
   `pipeline_empty`.
2. The adjudicator re-reads the primary documents for those cells and
   records what they say as claims of the source class `adjudication`,
   each with a quote and a locator. Such a claim is verified like any
   other, and is never attached to a style by the resolve stage.
3. The user runs the diff again. The script compares the adjudication
   claims with both sides and assigns the bucket.

The user adjudicates by hand until S3. In S3 the adjudicator is an agent
built on the Claude Agent SDK, under the same isolation rules. The diff
already reads adjudication claims; nothing writes them yet.

**Hand check.** On the development year the user reviews every
adjudicated cell and a sample of `gap_filled` cells, which have no
reference to catch an error. On the held-out years, a sample.

## Bars

| Measure | Bar |
|---|---|
| Style recall against the reference | at least 90% on the development year |
| Cell match on matched styles, after adjudication | at least 95% |
| Hand-check agreement with the adjudicator | at least 90% |
| Cells that resolve to a locator | 100% |
| Pipeline and reference error rates, gaps filled, cost per claim | reported |

Style recall is the headline number: a wrong cell can be found, a style
nobody found is invisible.

## The fix loop

An agent runs the pipeline, the user runs the diff, and an agent
receives the feedback file with the pipeline's own evidence. It may
change a prompt, a parser or the source list only when a primary
document shows the pipeline wrong, and the commit names the snapshot.

## Acceptance queries

1. "Which 2023 Toyota sedans cost the least?"
2. "Which 2023 Toyotas have over 300 horsepower, and what do they cost?"
   The power and the price must come from the same style.
