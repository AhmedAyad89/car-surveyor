# Lessons from the research phase

What the old repository taught, and the rule each lesson drives here.
File names refer to the old repository. Lessons specific to the German
market, RSS feeds or features stay there.

## Sources

- **Probe before depending.** The September plan relied on newsroom
  feeds, and 2 of 12 guessed feeds were live. The first restart plan
  chose Ford, which publishes no per-style prices. *Rule:* a probe with
  a yes or no and a count before any step relies on a site.
- **Guessed URLs measure the guesser.** No US feed was found by
  autodiscovery (`market_survey.md`). *Rule:* discovery crawls and
  records every candidate.
- **Sites refuse some clients.** Toyota's pressroom returns 403 to
  Python clients and 200 to curl. *Rule:* probe access per site, and
  never impersonate a browser.
- **Government data does not list styles.** fueleconomy.gov has no grade
  field, and one vPIC pattern covers four Camry grades. *Rule:* styles
  come from OEM documents; government data checks them.
- **Prior art moves fast.** OpenSpec went from one brand to 26 in under
  four months. *Rule:* pin the reference release.

## Extraction

- **Locate, never state.** Models produce plausible prices fluently
  (`evidence.py`). *Rule:* a value is admitted only when code finds it
  in the fetched document.
- **A precision fix can manufacture a false finding.** Tightening a euro
  pattern to exclude "Euro NCAP" also excluded every comma-grouped
  price, and produced the survey's headline conclusion, which was wrong
  (`market_survey.md`). *Rule:* every tightened pattern gets a test for
  what it must still match.
- **Tables need a table-aware reader.** A prose extractor found no
  prices in price lists, where the currency is in a column header
  (`market_survey.md`). *Rule:* the table-cell locator.
- **Arithmetic in the document is a free check.** Gross and net price
  columns exposed a wrong VAT rate in the project's own code. *Rule:*
  check the totals and pairs a document already contains.
- **Unstated is a value.** None of 120 sampled releases stated the VAT
  treatment. *Rule:* store `unstated`; never fill it with a convention.
- **Hedges are data.** "Up to" changes what a number means. *Rule:*
  store hedges as flags.
- **Resolve by ID, never by name.** vPIC's name lookup for "ford"
  returned 13 makes (`market_survey.md`). *Rule:* use `Make_ID`, and
  Toyota's model codes for styles.

## Evaluation

- **A missing row is worse than a wrong cell**
  (`v0_extraction_target.md`). *Rule:* style recall is the headline.
- **Agreement with a same-lineage source proves little.** Several
  candidate answer keys traced back to Edmunds. *Rule:* ask where a
  reference's values come from.
- **A system that settles its own disagreements is grading itself.**
  *Rule:* adjudication is checked by hand.

## Process

- **Scope creep came from widening.** Four quantities and eight tables
  made the September design unbuildable, and the first restart plan
  widened again. *Rule:* one make, one reference, six columns; additions
  wait for v0's gate.
- **Patched documents accumulate contradictions.** *Rule:* one document
  per purpose; rewrite instead of patching.
