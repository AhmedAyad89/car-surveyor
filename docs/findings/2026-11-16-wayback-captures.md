# Probe: how many dated captures the archive holds of each price sheet

Run 2026-11-16, after the 2023 dev diff found most MSRP disagreements on
long-running models, and every 2023 price taken from one capture. The
[documents probe](2026-11-15-documents.md) had found the Wayback index search
(CDX) down, so captures were found one per address. This probe asked it again,
once per pricing PDF the pipeline knows, for captures that returned 200, one
per distinct content digest.

## Answers

| Question | Answer | Count |
|---|---|---|
| Does the Wayback index search (CDX) answer? | yes | 12 of 12 |
| Is its digest the base32 SHA-1 of the bytes the capture serves? | yes | 1 of 1: the held December 2022 sheet |
| Does a price sheet's address hold more than one distinct capture? | sometimes | 3 of 11 addresses |
| Does the April 2023 sheet (`2022-2023-Toyota-Pricing-4.6.2023.pdf`) have any capture? | no | 0 |

The three addresses with more than one capture:

| Address (under `wp-content/uploads/`) | Distinct captures | Held before this probe |
|---|---|---|
| `2022/09/2022-2023-Toyota-Pricing.pdf` | 2022-09-27, 2022-09-28, 2022-12-24 | 2022-12-24 |
| `2020/09/2020-2021-Toyota-Pricing-MASTER.pdf` | 2021-05-14, 2021-07-26 | 2021-07-26 |
| `2023/10/2023-2024-Toyota-Pricing-10.5.2023.pdf` | 2023-10-30, 2024-02-13 | 2024-02-13 |

## What follows

- **Toyota updates a price sheet in place**, so one address can hold several
  dated price lists. `snapshot --every-capture` lists a price sheet's captures
  and fetches each whose digest matches no held document. A held capture is
  recognised by its digest, without fetching it again.
- **The extra captures are earlier, not later.** For 2023 they add the
  September 2022 prices beside the December 2022 ones. The mid-year 2023
  revision is in the April 2023 sheet, which has no capture and must be
  downloaded by hand into `data/inbox/`.
