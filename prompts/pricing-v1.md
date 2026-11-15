You are reading one Toyota full-line pricing document for a pipeline that rebuilds a vehicle catalogue from primary documents.

Everything you return is checked by code against the document. Each row you report is looked up in the table you name, by the row label you give, and a price that is not in that exact cell is discarded. So your job is to locate and copy, never to recall:

- Copy cells and quotes exactly as printed. Do not tidy spelling, spacing, or model names.
- If the document does not print something, return null. Do not fill a gap from what you know about Toyota's line-up.
- If the document looks wrong (the same model code on two rows, a price that seems off, a grade name that seems misplaced), report what it prints. Errors in the document are findings for a later stage, and correcting them here would hide them.

## What you are given

The document page by page. Each page shows its text, then each of its tables. A table is identified by its page number and its index on that page. Each table row is shown as its cells separated by ` | `; an empty cell is shown as nothing between two separators.

## What to return

**document_date**: the date the document says it was written or last updated, as `YYYY-MM-DD`, with the words that state it and the page they are on. Null if the document states no date.

**price_basis**: whether the listed prices include the delivery, processing and handling fee: `included`, `excluded`, or `unstated`. Give the sentence that says so and its page. Use `unstated` with a null quote when the document does not say.

**price_effective**: what the document says about when the listed prices take effect (for example a production month), as the sentence that says it, with its page. Null if it says nothing.

**delivery_fees**: one entry for each delivery, processing and handling fee the document states. For each: the amount as printed, the vehicle class as printed, the models the document lists for that class as printed, the date the fee takes effect as `YYYY-MM-DD` if the document gives one (else null), the line that states the amount and class, and the page.

**columns**: the header cells of the price tables as printed: the model code column, the model year column, the description column, and the price column. Null for a column the tables do not have.

**rows**: one entry for every priced row of every price table, in document order, for every model year in the document. Skip header rows and empty rows. For each row:

- `page`, `table`: where the row is.
- `row_label`: the cell that identifies the row. Use the model code when the row has one and no other row in the same table has the same code. Otherwise use the description cell.
- `model_code`, `model_year`, `description`, `msrp`: those cells exactly as printed. Null for `model_code` when the row has none.
- The description split into the fields below. Every word of the description should land in one of them, as printed. Leave a field null when the description has nothing for it.
  - `model`: the nameplate, such as `4Runner`, `Camry`, `RAV4`, `Corolla Cross`, `GR Supra`, `bZ4X`. Do not include words like Hybrid or Prime here.
  - `powertrain`: the electrified powertrain words if any, such as `Hybrid`, `Prime`, `Hybrid MAX`, `Fuel Cell`.
  - `grade`: the grade or edition, such as `SR5 Premium`, `SE Nightshade`, `TRD Off-Road`, `Woodland Special Edition`, `40th Anniversary`.
  - `drive`: the drive as printed, such as `4x2`, `4x4`, `FWD`, `AWD`.
  - `engine`: the engine designation, such as `V6`, `4-cyl`, `I4`, `i-FORCE MAX`.
  - `transmission`: the transmission designation, such as `5ECT`, `8ECTi`, `CVT`, `6AT`, `6MT`, `MT`, `AT`.
  - `body`: body, cab, bed, seating or door words, such as `Access Cab LB`, `CrewMax 5.5-ft. bed`, `8-Passenger`, `Liftback`, `Sedan 4-Door`.

Return every row. A missing row is a style the catalogue will never have, which is worse than a wrong cell.
