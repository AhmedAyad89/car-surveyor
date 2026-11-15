# Probe: fueleconomy.gov `vehicles.csv`

Run 2026-11-15 with curl 8.7.1 and its default user agent. The file was
parsed with Python's `csv` module.

## Answers

| Question | Answer | Count |
|---|---|---|
| Does `vehicles.csv` load? | yes | 50,647 rows, 84 columns |
| Are there Toyota rows for every model year 2020 to 2025? | yes | 465 rows |
| Do those rows state city, highway and combined mpg? | yes | 0 of 465 lack one |
| Is there a grade or trim column? | no | 0 of 84 columns |
| Could `robots.txt` be read? | no | 0 of 3 attempts |

## Per model year

Rows where `make` is `Toyota`.

| Model year | Rows | Distinct `model` strings |
|---|---|---|
| 2020 | 65 | 51 |
| 2021 | 67 | 54 |
| 2022 | 74 | 56 |
| 2023 | 78 | 59 |
| 2024 | 94 | 71 |
| 2025 | 87 | 65 |

## Notes

- **Address.** The download page `/feg/download.shtml` links
  `/feg/epadata/vehicles.csv.zip`: 2,228,192 bytes, one member, 21,909,747
  bytes unzipped. It returned 200 over HTTP/2.
- **Columns the pipeline needs** are present: `make`, `model`, `year`,
  `city08`, `highway08`, `comb08`, `trany`, `drive`, `displ`, `cylinders`,
  `fuelType`, `atvType`, `id`.
- **No grade field, but grades leak into `model`.** Some strings name a
  drive and a group of grades, such as `Camry AWD LE/SE` and
  `Camry Hybrid SE/XLE/XSE`. Joining a row to a style is a resolution step,
  as [sources.md](../plan/sources.md) says, and one row can cover several
  grades.
- **`robots.txt`** did not arrive: the HTTP/2 stream was cancelled twice and
  an HTTP/1.1 connection was reset once. The download page and the data
  file loaded normally in the same minutes.
- Host for the sandbox allowlist: `www.fueleconomy.gov`.
