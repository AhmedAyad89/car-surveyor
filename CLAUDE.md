# car-surveyor

Rebuilds a slice of OpenSpec, an open catalogue of US vehicle specifications,
from primary documents without seeing OpenSpec, then compares the two and
settles every disagreement against a primary document. v0 is Toyota, US, model
years 2020 to 2025. The plan is [docs/plan/README.md](docs/plan/README.md).

## Isolation

1. Do not read the reference data: nothing under `~/refdata/`, and no
   copy of the OpenSpec bundle anywhere. This covers opening, listing,
   searching, summarising and copying.
2. Do not fetch `visor.vin` or any subdomain, and do not search the web
   for OpenSpec data.
3. Do not ask for reference values, from the user or another agent.
4. Do not run `just diff` or `just reference-coverage`. The user runs
   them.
5. If you see reference data by accident, stop, do not use it, and tell
   the user where it was.
6. Every fix to a value rests on a primary document, named in the commit.

What you may know about OpenSpec is in
[docs/plan/sources.md](docs/plan/sources.md) and in its published schema under
`docs/reference_schema/`, which holds field names and no vehicle data.

## Commands

Always run through `uv` / `just`; never call `python` or `pip` directly.

- `just check` — lint, typecheck, test. Run before finishing any task.
- `just test -k <name>` — run a subset of tests.
- `just isolation-test` — exits 0 only when this session cannot reach the reference.
- `uv add <pkg>` / `uv add --dev <pkg>` — add a dependency in the step that
  first imports it, not before. Commit `uv.lock`.

## Naming

Casing follows PEP 8; the rules below are about *what kind of word* to use.

**Functions** — verb phrases. They do something.
- `fetch_snapshot()`, `parse_msrp()`, `normalize_for_matching()`.
- Verb tells the caller the cost: `get_` is cheap/local, `fetch_`/`load_` hits I/O, `build_`/`compute_` does work.
- Booleans-returning functions are predicates: `is_quote_present()`, `has_locator()`.

**Variables** — nouns. They hold something.
- Booleans read as predicates: `is_grounded`, `has_delivery_fee`, not `grounded`, `fee_flag`.

**Classes** — nouns for the thing, singular. `Snapshot`, `StyleClaim`, `Locator`.
- Avoid `-Manager`, `-Handler`, `-Helper` suffixes; they mean the responsibility is unclear.

**Modules** — nouns for what they contain, singular: `snapshot.py`, `evidence.py`, `storage.py`.
- A module named after a verb (`extract.py`) is usually one function in disguise; put it in the noun module it belongs to (`extraction.py`).

**Tests** — `test_<module>.py`; test functions describe behavior: `test_check_grounding_rejects_a_fabricated_quote`.

**Git**
- Branches: `<type>/<short-kebab-description>` — `feat/table-cell-locator`, `fix/msrp-parse`, `chore/ruff-config`.
- Types: `feat`, `fix`, `spike`, `chore`, `refactor`, `docs`, `test`.
- Never commit to `main` directly. Never force-push shared branches.

## Rules

- Type hints on every function signature. `ty` must pass.
- No network calls in tests; mock with `respx`.
- Secrets come from `.env` via `pydantic-settings`. Never hardcode, never commit.
- Notebooks, if any, go in `notebooks/` in the percent format, which represents notebooks as scripts.

## Data rules

- **Locate, never state.** A value is admitted only when code finds it where
  the model pointed in the fetched document.
- **Nothing invented, nothing overwritten.** A null means no source stated the
  value, and `unstated` is never replaced by a default.
- **Probe before depending** on any site or API.
