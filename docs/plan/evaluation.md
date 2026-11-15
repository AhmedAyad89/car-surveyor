# Evaluation

How the pipeline is scored against OpenSpec without any agent seeing
OpenSpec. Read [isolation.md](isolation.md) first.

## Splits

| Split | Contents | Feedback reaches code? |
|---|---|---|
| Development | Toyota, one model year (2023 by default) | yes, under the feedback contract |
| Held out | the other five model years | no; run and scored once, in S4 |

## The diff script

`diff.py` is deterministic and makes no model calls. The user runs it
with `just diff dev`.

- **Reads** the pipeline's views and `~/refdata/openspec/`.
- **Writes** the detailed diff to `~/refdata/out/`, and the feedback
  file to `data/feedback/<run_id>.json`.
- **Matches** MSRP when the reference value equals any dated pipeline
  value for the style, horsepower and torque within 5, mpg within 1.
- **Is tested** to prove the feedback file holds nothing the feedback
  contract forbids.

`just reference-coverage` prints counts of the reference alone.

## Row matching (open)

How a pipeline style is matched to a reference style is **not decided**.
Style names are not standardised, so some fuzzy matching is likely.
Decide it in S1, on real names. Constraints:

- It lives in the diff script and is deterministic.
- It reports how many styles matched exactly, matched approximately and
  did not match. A matching error looks like a missed style, so its rate
  has to be visible.
- Only the user can inspect individual matches, because that means
  seeing both sides. Agents tune it from the counts.
- Toyota's model codes are the preferred key if the reference carries
  them. The user checks.

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

## Adjudication

The adjudicator never sees a reference value.

1. The feedback file lists the cells flagged `disagree` or
   `pipeline_empty`.
2. The adjudicator re-reads the primary documents for those cells and
   records what they say as ordinary claims, each with a quote and a
   locator.
3. The user runs the diff again. The script compares the new claim with
   both sides and assigns the bucket.

The user adjudicates by hand until S3. In S3 the adjudicator is an agent
built on the Claude Agent SDK, under the same isolation rules.

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
