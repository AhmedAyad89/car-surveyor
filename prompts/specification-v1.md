You are reading one Toyota Product Information document for a pipeline that rebuilds a vehicle catalogue from primary documents.

Everything you return is checked by code against the document. Each value is looked up where you say it is, and a value whose quote is not found there, or whose number is not inside its quote, is discarded. So your job is to locate and copy, never to recall:

- Copy quotes exactly as printed. Do not tidy spelling or spacing.
- Report only what this document prints. Do not add a figure you know from elsewhere, and do not correct a figure that looks wrong.
- Never convert a unit or compute a figure. A hybrid's combined system output and an electric vehicle's MPGe are reported as printed, with the unit and the description the document gives.

## What you are given

The document page by page. Each page shows its text, then each of its tables. A table is identified by its page number and its index on that page. Each table row is shown as its cells separated by ` | `. The first row of a table is its header; a table continued from the page before has no header row of its own and is read under the header of the table it continues.

## What to return

**model_year** and **model**: the model year and model the document is about, as its title prints them.

**document_date**: the date the document says it was written or last updated, as `YYYY-MM-DD`, with the words that state it and the page. Null if it states none.

**values**: one entry for each figure the document states for these attributes:

- `horsepower`: engine or system power.
- `torque`: engine or system torque.
- `mpg_city`, `mpg_highway`, `mpg_combined`: EPA fuel economy estimates. A string such as `28/39/32` printed as city/highway/combined is three entries that share one quote. For an electric or plug-in vehicle, MPGe figures are reported under these attributes with the unit as printed.

Specification cells usually hold several figures, each followed by the grades it applies to. Report each figure as its own entry. For each entry:

- `attribute`: one of the five names above.
- `value`: the number as printed, such as `203` or `6,600`. Only the figure itself, not the rpm it is measured at.
- `unit`: the unit as printed next to it, such as `hp`, `lb-ft`, `lb.-ft.`, `MPG`, `MPGe`. For a fuel economy string with no unit beside it, use the unit the row label or header gives.
- `describes`: what the figure is when the document says more than the attribute name, such as `net combined system horsepower` or `gasoline engine`. Null otherwise.
- `quote`: the figure together with the words that scope it, exactly as printed, such as `203 hp @ 6,600 rpm (LE, SE, SE Nightshade, XLE)`. Keep it to that one figure and its scope; do not quote the whole cell.
- `applies_to`: what the document says the figure applies to.
  - `grades`: the grades listed for the figure, as printed, one per entry and without the drive word. Empty when the figure is given without a grade list, which means it applies to every grade in its column.
  - `drive`: the drive the figure is stated for, such as `AWD` or `FWD`, when the document ties the figure to one. Null otherwise.
  - `engine`: the engine the figure is stated for. In a specification table this is the header of the figure's column, as printed, such as `FOUR-CYLINDER` or `V6`. Null when the document has one engine and names none.
  - `transmission`, `body`: as printed, when the document ties the figure to one. Null otherwise.
- `hedges`: words that weaken or condition the figure, as printed, such as `up to`, `estimated`, `est.`, `preliminary`, or a footnote that says the estimate is for another model year. Empty when there are none.
- `locator`: where the figure is.
  - For a figure in a table: `kind` is `table_cell`, with the `page`, the `table` index, the `row_label` (the first cell of the figure's row, as printed) and the `column_header` (the header cell above the figure, as printed).
  - For a figure in running text: `kind` is `text`, with the `page`. Leave the table fields null.

If the document states no figure for an attribute, return nothing for it. An empty list is a correct answer for a document with no such figures.
