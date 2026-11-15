# car-surveyor

A practice project for LLM extraction pipelines, coding agents and the Claude
Agent SDK.

OpenSpec is an open catalogue of US vehicle specifications published by Visor.
This project rebuilds a slice of it from primary documents without seeing
OpenSpec, then compares the two and settles every disagreement against a
primary document. v0 covers Toyota, US, model years 2020 to 2025, and reports
the pipeline's error rate beside OpenSpec's on the same slice.

The plan is in [docs/plan/](docs/plan/README.md). The repository is at step S0:
tooling, isolation and probes. No pipeline stage exists yet.

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

A recipe is added with the code it runs, so the pipeline stages arrive from S1.

## Isolation

The project scores itself against OpenSpec, so nothing that builds or runs the
pipeline may see OpenSpec's data. The reference lives outside the repository,
in `~/refdata/`, and `.claude/settings.json` keeps agents away from it and from
`visor.vin`. The rules and the enforcement are in
[docs/plan/isolation.md](docs/plan/isolation.md).

`just isolation-test` tries three routes to the reference. Run by an agent it
must report every route blocked and exit 0. Run from your own terminal it
reports them open and exits 1, which proves the test is real.

The Python package is `automotive_surveyor`, the name the plan gives it.
