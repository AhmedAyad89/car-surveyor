# Probe: Toyota pressroom

Run 2026-11-15 with curl 8.7.1 from a developer machine. Requests used
curl's default user agent unless stated, and were spaced 10 seconds apart
once `robots.txt` had been read. No header was added to get past a refusal.

## Answers

| Question | Answer | Count |
|---|---|---|
| Does the pressroom serve curl? | yes | 14 of 14 requests returned 200 |
| Does it serve Python's default user agent? | no | 1 of 1 returned 403 |
| Does it serve Python with a descriptive user agent? | yes | 1 of 1 returned 200 |
| Does `robots.txt` permit crawling? | yes | 0 paths disallowed |
| Does `robots.txt` set a pace? | yes | `Crawl-delay: 10` |
| Is there a full-line pricing page? | yes | 8 PDFs linked |
| Can curl download those PDFs? | **no** | 8 of 8 returned 403 |
| Can curl download a Product Information PDF? | **no** | 1 of 1 returned 403 |
| Does the Wayback Machine hold the 8 linked PDFs? | partly | 3 of 8 |

The Python results are on different pages: the default user agent was
refused on `/`, the descriptive one was served `/robots.txt`. Confirm on a
content page before relying on it.

## Where the documents are

- **Discovery.** `robots.txt` names `sitemap_index.xml`, which lists 63
  sitemaps. Six post sitemaps hold 5,382 release URLs and the vehicle
  sitemap holds 235 vehicle pages.
- **Pricing.** The post `/toyota-full-line-pricing/`, last modified
  2026-11-05, links 8 PDFs. Each covers two adjacent model years. Release
  slugs are a poor index of pricing: 96 of 5,382 mention a price, and for
  model years 2023 to 2025 only 1 does.
- **Specifications.** A vehicle page (`/vehicle/2023-toyota-camry/` was the
  sample) states no figures itself: no MSRP, horsepower, torque or mpg in
  its text, and no tables. It links one Product Information PDF, one
  full-line pricing PDF and one release.
- **Host.** Every PDF is on `toyota-cms-media.s3.amazonaws.com` or
  `s3.amazonaws.com`, not on the pressroom. That host answered
  `AccessDenied` to all 9 files tried, to GET and to HEAD.

## Per model year

| Model year | Full-line PDFs naming it | Held by Wayback | Vehicle pages | Release slugs naming the year |
|---|---|---|---|---|
| 2020 | 2 | 2 | 24 | 92 |
| 2021 | 2 | 2 | 24 | 79 |
| 2022 | 2 | 1 | 25 | 92 |
| 2023 | 2 | 0 | 27 | 70 |
| 2024 | 2 | 0 | 30 | 87 |
| 2025 | 1 | 0 | 29 | 74 |

Release slugs naming a year are an upper bound: the count includes any
release with that year in its address.

The full-line PDFs, as linked on 2026-11-15:

| Link text | File | Wayback capture |
|---|---|---|
| 2019 - 2020 Full-Line Pricing | `2019-2020-Toyota-Pricing-MASTER.pdf` | 2022-09-28 |
| 2020 - 2021 Full-Line Pricing | `2020-2021-Toyota-Pricing-MASTER.pdf` | 2021-07-26 |
| 2021 - 2022 Full-Line Pricing | `2021-2022-Toyota-Pricing-2.1.22.pdf` | 2022-02-01 |
| 2022 - 2023 Full-Line Pricing | `2022-2023-Toyota-Pricing-4.6.2023.pdf` | none |
| 2023 - 2024 Full-Line Pricing | `2023-2024-Toyota-Pricing-1.05.2024.pdf` | none |
| 2024 - 2025 Full-Line Pricing | `2024-2025ToyotaPricing-03282025.pdf` | none |
| 2026 Full-Line Pricing | `2026_Toyota_Pricing_02262026-2.pdf` | none |
| Click here to Download | `2026_and_2027_Toyota_Pricing_11052026.pdf` | none |

Two more Wayback captures exist for PDFs the 2023 Camry page links: an
earlier edition of the 2022 to 2023 pricing PDF
(`2022/09/2022-2023-Toyota-Pricing.pdf`, captured 2022-12-24) and the 2023
Camry Product Information PDF (captured 2023-02-04). The file names carry
edition dates, so one model year has several editions at different
addresses.

## Not established

- What any PDF contains. None was downloaded, so the claim in
  [sources.md](../plan/sources.md) that pricing documents carry one row per
  model code and state the delivery fee is still unchecked here.
- Whether the storage host serves a browser, or refuses everyone.
- Whether Wayback holds other editions. Only exact addresses were looked up.

## What follows

- The fetcher waits 10 seconds between pressroom requests.
- A Python client with an honest user agent may be enough; curl is not
  required by this evidence.
- The primary pricing and specification documents cannot be fetched by the
  method that works for the pressroom. How they are obtained is open:
  Wayback copies where they exist, files downloaded by hand, or another
  method the user accepts.
- Wayback coverage of the linked pricing PDFs is complete for model years
  2020 and 2021, partial for 2022 and absent for 2023 to 2025, though an
  earlier 2022 to 2023 edition is held. That bears on the choice of
  development year.
- Host for the sandbox allowlist: `pressroom.toyota.com`.
- **Wayback is also a route to the reference.** The capture index is on
  `archive.org` and captures are served from `web.archive.org`, which was
  not fetched. The Wayback Machine archives `visor.vin` as well, and a deny
  rule for `visor.vin` does not match an archived copy at a
  `web.archive.org` address. Allowing these hosts to agents opens that
  route, so the fetcher should accept a Wayback address only when the
  archived address is a Toyota one, with a test.
