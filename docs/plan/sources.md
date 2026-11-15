# Reference and sources

The **reference** is what the pipeline is compared with. **Sources** are
what the pipeline reads. Everything here was checked on the live web on
2026-11-15 unless marked as a probe.

## The reference: OpenSpec

- **Publisher and licence.** Visor, at `openspec.visor.vin`. The
  database is under ODbL 1.0; the schema and tooling under Apache 2.0.
- **Age.** First released 2026-07-30 with one brand. Release 2026-11-09
  has 26. Toyota was added on 2026-09-21.
- **Shape.** One file per make, model and model year. A file holds
  trims, and each trim holds configurations. Values inherit from model
  to trim to configuration. MSRP sits on the configuration and excludes
  the destination charge. Some years have a separate mid-year file.
- **Provenance.** Source documents are listed per file. Nothing links a
  single value to a document.
- **Time.** One value per attribute, and one `last_updated` per file.
- **Completeness.** Each section is declared `partial` or `full`, and
  most are `partial`. A value missing from a `partial` section is
  unknown, not absent.
- **Review.** A person approves each model year, not each value. No
  value error rate is published.

What follows:

- **Pin the release.** Files change in most releases. Development uses
  2026-11-09. Before reporting an error to Visor, the user re-runs the
  diff against the latest release.
- **Corrections** go through the "Suggest correction" form on each
  value, which takes a source URL.
- **Licence.** Publishing metrics carries no obligation. Publishing
  OpenSpec records together with corrected values must be under ODbL,
  with attribution to OpenSpec.

**Probe:** OpenSpec's Toyota coverage for 2020 to 2025 is not known. The
user runs `just reference-coverage`, which prints counts only.

## Sources

| Source | Gives |
|---|---|
| Toyota pressroom, `pressroom.toyota.com` | styles, MSRP, delivery fee, horsepower, torque |
| fueleconomy.gov file `vehicles.csv` | city, highway and combined mpg |
| vPIC VIN decoding, optional | a cross-check on grade names and engine power |
| Wayback Machine | a fallback copy of a pressroom page, and its capture date |

### Toyota pressroom

- Pricing releases carry one row per model code with the MSRP, and state
  the delivery, processing and handling fee. A "full-line pricing" page
  holds full-line PDFs back to 2019 or 2020.
- A model year is assembled from several documents. Some releases price
  only a new grade, a grade can be priced later than the rest of its
  model, and prices change mid-year.
- Python HTTP libraries (urllib, requests, httpx) received HTTP 403.
  curl with its default user agent received 200. Use a client the site
  accepts and `robots.txt` permits, identify it honestly, and never
  impersonate a browser. If the site refuses, use the Wayback copy.

**Probe:** documents per model year for 2020 to 2025, the fetch method
that works, and what `robots.txt` allows.

### Others

- **fueleconomy.gov.** `vehicles.csv` has mpg per model, engine,
  transmission and drive. It has no grade field, so joining it to styles
  is a resolution step, not a lookup.
- **vPIC.** A cross-check only. `Trim` often holds several grades in one
  string, `EngineHP` may differ from the real figure by up to 10%, and
  `BasePrice` is one figure per VIN pattern. Resolve the make by
  `Make_ID`, never by name. New York State's open registration data is a
  source of VINs.
- **Wayback Machine.** In practice about 30 index queries a minute.
  Pause 60 seconds after an HTTP 429, and keep volumes low.

## Not used

| Source | Why |
|---|---|
| Ford and other makes | Ford published only a "starting at" price per model. Per-style prices exist only in leaked dealer price lists |
| CarAPI | dropped by the user. Its free data ends at model year 2020 |
| EPA test car list | its horsepower differs from manufacturer figures by tens of horsepower on some engines |
| Natural Resources Canada, SAE J1349 list | Canadian names and units; paid documents with no Toyota |
| Window stickers | no public route was found for Toyota |
| Aggregators, review sites, dealer listings | not primary. Not used as sources, and not as hints about which styles exist |

No free government source carries torque for light-duty vehicles, so
torque comes from Toyota's documents only.
