# car-surveyor

A practice project for LLM extraction pipelines, coding agents and the Claude
Agent SDK.

OpenSpec is an open catalogue of US vehicle specifications published by Visor.
This project rebuilds a slice of it from primary documents without seeing
OpenSpec, then compares the two and settles every disagreement against a
primary document. v0 covers Toyota, US, model years 2020 to 2025, and reports
the pipeline's error rate beside OpenSpec's on the same slice.

The plan is in [docs/plan/](docs/plan/README.md), how the pipeline works is in
[docs/plan/design.md](docs/plan/design.md), and how it is scored is in
[docs/plan/evaluation.md](docs/plan/evaluation.md).

## Setup

Needs [uv](https://docs.astral.sh/uv/), [just](https://just.systems/) and
[prek](https://github.com/j178/prek).

```
uv sync
prek install
just check
```

## Commands

| Recipe | Does |
|---|---|
| `just check` | lint, typecheck and test |
| `just fmt` | fix lint findings and format |
| `just test -k <name>` | run a subset of tests |
| `just isolation-test` | proves a session cannot reach the reference |
| `just discover` | records every document the pressroom's listing pages link to |
| `just snapshot` | fetches each recorded document once and keeps it under `data/` |
| `just extract` | reads each snapshot once: one model call per document, a parser for the fuel economy file |
| `just verify` | admits or rejects each extracted claim against its document |
| `just resolve` | builds one row per style from the admitted claims, then runs the consistency checks |
| `just run dev` | runs every stage in order for the development year (`heldout` and `all` also work) |
| `just status` | counts recorded documents by kind and by what became of them |
| `just diff dev` | **you only, from your own terminal:** compares the styles with OpenSpec |
| `just reference-coverage` | **you only:** counts what OpenSpec holds per model year |

The stage recipes take `--years 2023` to work on some model years only, and
`--retry-unavailable` to ask again for what an earlier run could not get. A
second run of a stage makes no requests and no model calls.

`just extract` calls the Claude API, with the key from `ANTHROPIC_API_KEY` in
`.env` or an `ant auth login` profile. The model is `claude-opus-5-5` unless
`CAR_SURVEYOR_EXTRACTION_MODEL` says otherwise. Without credentials, another
model runner can read the documents through files:

```
just extract --export data/extraction --label <runner-name>
# answer each <id>.request.json as <id>.answer.json, then
just extract --import data/extraction --label <runner-name>
```

Either way an answer is kept under its snapshot and extractor version, and
passes the same schema and the same verification.

Toyota's PDFs sit on a host that refuses every client but a browser. The
pipeline takes each from its Wayback Machine capture. Where the archive holds
none, the document is `unavailable`. `just status --missing` lists each one
with its address: download it in a browser, put it in `data/inbox/` under its
original file name, and run `just snapshot` again.

## Scoring against OpenSpec

`just diff dev` reads the OpenSpec bundle at `~/refdata/openspec/` and writes
two files. `~/refdata/out/<run_id>.json` holds both sides' values and is for
you. `data/feedback/<run_id>.json` holds a status per cell and counts, with no
OpenSpec value in it, and is what an agent may read. `just diff heldout` writes
only the first, so the held-out years stay unseen until the end.

The command refuses to start inside a Claude Code session. Run it in a plain
terminal.

A recipe is added with the code it runs.

## Isolation

The project scores itself against OpenSpec, so nothing that builds or runs the
pipeline may see OpenSpec's data. The reference lives outside the repository,
in `~/refdata/`, and `.claude/settings.json` keeps agents away from it and from
`visor.vin`. The rules and the enforcement are in
[docs/plan/isolation.md](docs/plan/isolation.md).

`just isolation-test` tries three routes to the reference. Run by an agent it
must report every route blocked and exit 0. Run from your own terminal it
reports them open and exits 1, which proves the test is real.
