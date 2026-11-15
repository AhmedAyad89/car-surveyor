# Probe: what the primary documents contain and how they are reached

Run 2026-11-15, after the [pressroom probe](2026-11-15-pressroom.md), which
left open what any PDF contains and how to obtain one. Requests identified
the project by its repository address and kept to the pressroom's 10-second
crawl delay. No header was added to get past a refusal.

## Answers

| Question | Answer | Count |
|---|---|---|
| Does the pressroom serve a Python client with an honest user agent, on a content page? | yes | 1 of 1 returned 200 |
| Does the media host serve this client anything at all? | **no** | 0 of 2: an image and a PDF both returned 403 |
| Does the Wayback Machine serve a capture of a pricing PDF? | yes | 1 of 1, 186,713 bytes |
| Does it serve a capture of a Product Information PDF? | yes | 1 of 1, 272,660 bytes |
| Does the Wayback index search (CDX) answer? | **no** | 0 of 3: HTTP 503, 500, 503 |
| Does the Wayback availability lookup answer? | yes | 1 of 1 |
| Does a pricing PDF carry one row per model code with an MSRP? | yes | 8 pages, about 250 rows |
| Does it state the delivery fee? | yes | 4 vehicle classes, with an effective date |
| Does a PDF reader find those rows as tables? | yes | 2 tables on page 1 |
| Does a Product Information PDF state horsepower, torque and mpg? | yes | per engine, qualified by grade |
| Do pressroom releases carry price tables in their HTML? | no | 0 tables in 4 of 4 releases read |

## What follows

- **The fetcher is plain Python.** curl is not needed.
- **The media host refuses everything, not only PDFs.** Every pricing and
  specification document is on it, so each comes from a Wayback capture or
  from a file downloaded by hand into `data/inbox/`. The registry marks the
  host `route: archive` and the fetcher refuses to ask it directly.
- **Captures are found by the availability lookup**, one address at a time.
  It returns one capture per address, so other editions of a document are
  found only if a pressroom page still links their address. The index search
  that would list every capture was down; it is not relied on.
- **Pricing PDFs are the source of styles and MSRP.** The capture of the
  2022 to 2023 edition is dated 2022-12-22 inside the document and holds the
  2023 line-up as priced by then.
- **Releases are prose.** They state horsepower, mpg and some starting prices
  in sentences, and no price table, so they are a second source for a few
  cells and not a source of styles.

## What a pricing PDF looks like

One table per page with the header `MODEL #`, `MY`, `MODEL (in alphabetical
order)`, `MSRP`. A row reads like `8642 2023 4Runner 4x2 SR5 V6 5ECT
$39,555`: Toyota's model code, the model year, a description that runs the
model, drive, grade, engine and transmission together, and the price. The
first page states that prices exclude the delivery, processing and handling
fee, and gives that fee for four vehicle classes with the date it took
effect. Each page ends with `Last updated` and a date.

Wrinkles seen in the one document read, each of which the pipeline has to
survive without inventing anything:

- Rows for two model years are mixed in one table.
- Some rows have no model code.
- One model code appears on two rows with different grades and prices.
- One row's description wraps onto a second line, splitting the code from
  the price.
- The description's word order varies between rows of one model.
- A read PDF table has more columns than the page shows, and the header sits
  one column to the left of the values under it.

## What a Product Information PDF looks like

A specification table with one column per engine. A cell can hold several
values, each followed by the grades it applies to, such as a horsepower
figure for four named grades and another for their all-wheel-drive versions.
Fuel economy is given the same way, as city, highway and combined in one
string per group of grades. A value therefore applies to a set of styles,
and attaching it to each style is a resolution step.

## Not established

- How many of the pricing and Product Information PDFs for 2020 to 2025 the
  Wayback Machine holds. The snapshot stage records this per document as it
  runs, and `just status` prints the counts.
- Whether later editions of the 2023 pricing exist in the archive. The
  pressroom links two editions for 2022 to 2023; one was fetched.
