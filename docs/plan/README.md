# Restart plan for automotive-surveyor

Written 2026-11-15. This folder plans a **new repository** that succeeds
the September 2026 research repository ("the old repository", where this
folder was first written). It replaces every earlier planning document.

This file is the plan at headline level. Detail is in the
[other files](#files).

> **Agents: read [isolation.md](isolation.md) first.** Never read
> `~/refdata/` or any copy of the OpenSpec data, never fetch `visor.vin`
> or its subdomains, and never ask for a reference value.

## What this is

A practice project for LLM extraction pipelines, coding agents and the
Claude Agent SDK.

OpenSpec is an open catalogue of US vehicle specifications published by
Visor. v0 rebuilds a slice of it from primary documents **without seeing
OpenSpec**, then compares the two and settles every disagreement against
a primary document.

| | OpenSpec | This project |
|---|---|---|
| Provenance | documents listed per file | per value: document, location, quote |
| Time | one value per attribute | dated values, history kept |
| Sources that disagree | one value shown | every value kept |
| Accuracy | no value error rate published | measured and reported |

v0 reports the pipeline's error rate, OpenSpec's error rate on the same
slice, the gaps the pipeline fills, and the styles each side lacks.

## Scope of v0

- **Make and market:** Toyota, US.
- **Model years:** 2020 to 2025. One is the development year. The other
  five are held out and scored once, at the end.
- **Rows:** styles. A style is one priced configuration of a model in a
  model year: grade, body, engine, transmission, drive.
- **Columns:** MSRP with the delivery fee, horsepower, torque, and city,
  highway and combined mpg.
- **Reference:** OpenSpec release 2026-11-09, read only by the diff
  script.
- **Sources:** Toyota's pressroom and fueleconomy.gov.

## Pipeline

| Stage | Does |
|---|---|
| `discover` | lists Toyota's pricing and specification documents |
| `snapshot` | fetches each document once and keeps it |
| `extract` | one model call per document returns styles and values, each with the place it was found |
| `verify` | code confirms each value at that place, then runs consistency checks |
| `resolve` | builds the query views |
| `diff` | a plain script, run by the user, compares the views with OpenSpec and writes a coarse feedback file |
| `view` | a local page for inspecting the data and the diff |

## Rules

1. **Isolation.** Only the diff script reads the reference.
2. **Locate, never state.** A value is admitted only when code finds it
   where the model pointed in the fetched document.
3. **Nothing invented, nothing overwritten.** A null means no source
   stated the value, and `unstated` is never replaced by a default.
4. **A fix rests on a primary document**, never on the reference.
5. **Probe before depending** on any site or API.

## Steps

| Step | Builds | Gate |
|---|---|---|
| S0 | repository, isolation, probes | `just check` and `just isolation-test` pass; every probe has a yes or no and a count |
| S1 | one model end to end: styles and MSRP, through the diff | a second run changes nothing and makes no fetches or model calls; the row-matching rule is written down |
| S2 | all Toyota models for the development year, all six columns | every value traces to a document |
| S3 | consistency checks, adjudication, the fix loop | seeded errors are caught; bars are met or each miss is explained |
| S4 | freeze, then run the five held-out years once | held-out numbers reported beside the development numbers |

v0 is done when S4's gate passes.

## Decisions

**Made by the user, 2026-11-15**

- A new repository. The old one is kept as the research record.
- Toyota, US, model years 2020 to 2025.
- OpenSpec is the only reference. CarAPI is dropped.
- The diff is a plain script.
- The repository is `car-surveyor` and the Python package is
  `car_surveyor`.

**Made while building S1, 2026-11-16, for the user to overrule**

- The development year is 2023, the plan's default.
- Reference rows are matched to pipeline styles by Toyota's model code,
  which the reference carries, and then by description. See
  [evaluation.md](evaluation.md#row-matching).
- The dashboard is a local viewer over the database, not an Evidence
  site. See [design.md](design.md#viewer).

**Proposed in review, to confirm**

- OpenSpec's published schema file may sit in the repository. Its data
  may not.
- One agent in v0, the adjudicator in S3.

**Open**

1. A time box and a spend cap per step.
2. Whether confirmed OpenSpec errors are sent to Visor.

**Not in v0:** other makes, options and features, automatic repair of
detected problems, an MCP server, sales data, other markets.

## Files

| File | Holds |
|---|---|
| [bootstrap.md](bootstrap.md) | creating the repository: what to copy, tooling, layout, the S0 checklist |
| [isolation.md](isolation.md) | rules for agents, enforcement, the feedback contract |
| [sources.md](sources.md) | what is known about OpenSpec and about each source |
| [design.md](design.md) | data model, evidence check, consistency checks, dashboard |
| [evaluation.md](evaluation.md) | splits, the diff script, adjudication, bars |
| [lessons.md](lessons.md) | what the research phase taught, and the rule each lesson drives |
