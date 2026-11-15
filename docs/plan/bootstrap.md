# Bootstrap: creating the new repository

For the agent that creates the repository. This is step S0.

Start the session in the new repository, with the old one added for
reading: `c <old repository>`. The isolation settings
apply only to sessions started in the new repository.

## 1. Done by the user first

- Create the empty repository on GitHub and clone it.
- Move the OpenSpec bundle to `~/refdata/openspec/`, remove the copies
  in `~/Downloads/`, and create a canary file `~/refdata/CANARY`.
- Remove the CarAPI files: `~/Downloads/carapi-*` and the old
  repository's `docs/carapi-feed-sample-10rows.xlsx`. Never commit them.
- Place OpenSpec's published schema file in the new repository under
  `docs/reference_schema/`.
- In the old repository, commit the pending changes and this folder,
  merge into `master`, and leave it read-only.

## 2. Copy from the old repository

| Item | Action |
|---|---|
| `pyproject.toml` | copy; write the description; remove `[project.scripts]` until `cli.py` exists, and the `ty` comment; set ruff `target-version` to `py314`; replace the dependencies with section 3 |
| `Justfile` | copy; remove `survey`; add the recipes in section 5 |
| `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `.gitignore`, `.python-version` | copy unchanged; run `prek install` |
| `claude.md` | copy as `CLAUDE.md`, capitalised; edit per section 6 |
| `evidence.py` and its tests | port `normalize_for_matching`, `is_quote_present`, `check_grounding` and their eight tests, with the German fixtures rewritten on Toyota pricing text. Leave the corroboration half. The extension in [design.md](design.md#the-evidence-check) is S1 work |
| this folder | copy as `docs/plan/` |
| everything else | do not copy; `uv.lock` is generated |

## 3. Dependencies

Add a dependency in the step that first imports it, not before.

- **Development, from S0:** pytest, ruff, ty.
- **Runtime, as the steps reach them:** httpx, with respx for its
  tests; pydantic; pydantic-settings; duckdb; anthropic.
- **Only if needed:** a PDF reader (pdfplumber is MIT, PyMuPDF is
  AGPL); trafilatura, never for locating a value
  ([design.md](design.md#the-evidence-check)); Evidence, the dashboard
  tool, a Node package, in S1; the Claude Agent SDK in S3.
- **Not carried:** feedparser, pandas, jupytext, ipykernel, pytest-cov.

How each source is fetched is in [sources.md](sources.md#toyota-pressroom).

## 4. Layout

The layout at the end of v0. Each step creates the files it fills; S0
creates no empty modules.

```
CLAUDE.md  README.md  Justfile  pyproject.toml  uv.lock
.claude/settings.json        # isolation rules
sources.yaml                 # the source registry
prompts/
sql/views.sql
docs/plan/                   # this folder
docs/findings/               # probe results and diff reports, dated
docs/reference_schema/       # placed by the user
src/car_surveyor/
  source.py  snapshot.py  extraction.py  evidence.py
  verification.py  resolution.py  storage.py  cli.py
  reference.py               # loads ~/refdata; for diff.py and
  diff.py                    #   reference-coverage only
tests/
dashboard/                   # added in S1
data/                        # gitignored: snapshots/, surveyor.duckdb, feedback/
```

## 5. Recipes

A recipe is added with the code it runs. S0 has `check`, `fmt`, `test`,
`isolation-test` and `reference-coverage`.

- `check`, `fmt`, `test *args`: as in the old repository.
- `discover`, `snapshot`, `extract`, `verify`, `resolve`: the stages.
  `run dev` runs them all for a split. The data stage is `verify` so
  that it does not collide with `check`.
- `report`: run metrics and the dashboard build.
- `isolation-test`: see [isolation.md](isolation.md#isolation-test).
- `diff dev` and `reference-coverage`: **run by the user only.**

## 6. `CLAUDE.md`

Under about 80 lines, and only what is not obvious.

1. One paragraph of purpose and a link to `docs/plan/README.md`.
2. The isolation rules, copied from
   [isolation.md](isolation.md#rules-for-agents).
3. Commands: always `uv` and `just`; `just check` before finishing.
4. The naming section from the old `claude.md`, without "Remotes and
   resources". Rewrite its examples, which are about listings and
   scrapers, in this project's terms, and name the default branch the
   new repository has.
5. Carried rules: type hints on every signature; no network calls in
   tests; secrets from `.env`; notebooks, if any, in percent format.
6. Data rules: rules 2, 3 and 5 in [README.md](README.md#rules). Rules
   1 and 4 are already in the isolation rules.

## 7. S0 checklist

1. Write `.claude/settings.json` and `just isolation-test`, then start
   a new session. The test fails for an agent and passes for the user.
2. Create the rest of the repository from sections 2 to 6. `just check`
   passes in CI.
3. Write `reference.py` and `reference-coverage` against the schema and
   synthetic fixtures.
4. Run the probes. Write each result to `docs/findings/` with a yes or
   no and a count:
   - pressroom pricing and specification documents per model year, the
     fetch method that works, and what `robots.txt` allows;
   - `vehicles.csv` loads, with Toyota rows per model year;
   - the user runs `just reference-coverage` and shares the counts.
5. The user picks the development year and sets a time box and a spend
   cap for S1.
