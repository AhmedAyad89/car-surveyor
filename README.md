# car-surveyor

A practice project for LLM extraction pipelines, coding agents and the Claude
Agent SDK.

OpenSpec is an open catalogue of US vehicle specifications published by Visor.
This project rebuilds a slice of it from primary documents without seeing
OpenSpec, then compares the two and settles every disagreement against a
primary document. v0 covers Toyota, US, model years 2020 to 2025, and reports
the pipeline's error rate beside OpenSpec's on the same slice.

**Start with [docs/pipeline.md](docs/pipeline.md)**: each step of the
pipeline, what it reads and writes, and where Toyota is built in. The plan
is in [docs/plan/](docs/plan/README.md), the finer rules behind each step
are in [docs/plan/design.md](docs/plan/design.md), and how the pipeline is
scored is in [docs/plan/evaluation.md](docs/plan/evaluation.md).

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
| `just view` | serves a local page over the database, the documents and the diff's feedback, for development |

The stage recipes take `--years 2023` to work on some model years only, and
`--retry-unavailable` to ask again for what an earlier run could not get. A
second run of a stage makes no requests and no model calls.

After a change to how documents are read, `just snapshot --rederive` derives
every held document's text and tables again from its stored bytes, without
fetching, and `just verify --recheck` checks every claim against the result.

`just extract` calls the Claude API, with the key from `ANTHROPIC_API_KEY` in
`.env.secret` or an `ant auth login` profile. The model is `claude-haiku-5-5`
with thinking off, at effort `high`, unless `CAR_SURVEYOR_EXTRACTION_MODEL`,
`CAR_SURVEYOR_EXTRACTION_IS_THINKING` or `CAR_SURVEYOR_EXTRACTION_EFFORT` say
otherwise. Opus 5.5, Sonnet 5.5 and Fable models need thinking on, and thinking
can be off only at effort `high` or below; the command refuses to start
otherwise. Answers read without thinking are kept apart from answers read with
it.

The API reads **price sheets only**, unless `--kind` names other kinds. Spec
sheets are read by Claude Code agents running Opus, through files, with the
label `claude-code-opus`: Haiku located their figures too poorly to admit
([comparison](docs/findings/2026-11-16-haiku-vs-opus-extraction.md)). The same
route serves any model runner without credentials:

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

Toyota also updates a price sheet in place at the same address. `just snapshot
--every-capture` asks the archive for every distinct capture of each held price
sheet and keeps those it does not hold, so earlier prices become dated values
beside the later ones. Run `extract`, `verify` and `resolve` afterwards.

## Scoring against OpenSpec

`just diff dev` reads the OpenSpec bundle at `~/refdata/openspec/` and writes
two files. `~/refdata/out/<run_id>.json` holds both sides' values and is for
you. `data/feedback/<run_id>.json` holds a status per cell and counts, with no
OpenSpec value in it, and is what an agent may read. `just diff heldout` writes
only the first, so the held-out years stay unseen until the end.

The command refuses to start inside a Claude Code session. Run it in a plain
terminal.

## Viewer

`just view` serves a page at `http://127.0.0.1:8765/` (`--port` picks another
port) with the catalogue, each cell's claims and evidence, the documents, the
checks and the diff's feedback. It only reads, so the pipeline can run beside
it, and it shows no OpenSpec value. To view another data directory, set
`CAR_SURVEYOR_DATA_DIR`.

From your own terminal, `just view --diff-detail ~/refdata/out/<run_id>.json`
also shows OpenSpec's values beside the pipeline's, at the address with a
token that it prints. An agent session is refused that argument.

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
