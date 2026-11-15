# Isolation during the S1 build

The session that built the pipeline stages, the diff and the viewer ran
on 2026-11-15 and 2026-11-16. This is what happened to the isolation
rules in it, for the record the rules ask for.

## Answers

| Question | Answer |
|---|---|
| Was the session sandboxed? | **no** |
| Was any reference value seen? | no |
| Were reference file names seen? | yes: top-level names of the bundle, once |
| Was `~/refdata/` read, listed or searched? | no |
| Was `visor.vin` fetched? | no |
| Were `just diff`, `just reference-coverage` or `just isolation-test` run? | no |
| Was the reference loader run on real reference files? | no |
| Does `~/refdata/CANARY` exist? | no, so `just isolation-test` still cannot judge the file routes |

## What happened

- **The session was not sandboxed.** It started in the main checkout,
  which was still on the repository's first commit and so had no
  `.claude/settings.json`. Settings are read when a session starts. The
  rules were kept by hand from then on.
- **The bundle's top-level names were listed once.** The first directory
  listing of the session, made before the rules were read, showed the
  untracked `docs/openspec-2026-11-09/` folder to a depth of three:
  licence files, `bundle.json`, three schema file names and a `data/us`
  folder. No make, model, file content or value was shown. The folder was
  not opened again.
- **The bundle was moved, and the schema copied, by a side agent.** While
  the build ran, the user asked a side agent of the same session to pull
  the schema out to the appropriate folder and move the data. That agent
  reports that it moved `docs/openspec-2026-11-09/` to
  `~/refdata/openspec/` as one folder without opening or listing it, and
  copied only its `schema/` folder to `docs/reference_schema/`. The main
  session checked the result by existence only: the folder is gone from
  the checkout, `~/refdata/openspec` exists, and `docs/reference_schema/`
  holds twelve schema files.
- **The schema was read.** `lineup`, `fuel_economy`, and the parts of
  `structure` and `powertrain` the loader needs. They hold field
  definitions and no vehicle data.
- **Documents were read by subagents.** Each was told to read only its
  own request files, never `~/refdata/` or any folder named `openspec`,
  and to copy only what its document prints.
- **A request was refused for carrying the user's email.** The first
  probe of the session put the user's email address in a User-Agent
  header. The request was blocked before it left. The fetcher identifies
  the project by its repository address and nothing else.

## What changed because of it

- `diff` and `reference-coverage` refuse to start when `CLAUDECODE` is
  set, which Claude Code sets in every agent shell. The sandbox is the
  barrier that matters. This one covers a session that starts without
  it, as this one did.
- The viewer shows a reference value only when started with an explicit
  `--diff-detail` file, has no default path, and refuses that argument
  in an agent session.
- Neither the diff nor the loader has a default path into `~/refdata/`.
  The Justfile passes it, so a test cannot reach it by accident.

## Open

1. **Pull the main checkout** before starting another session there:
   `git -C ~/code/car-surveyor pull`. Until then a session started in it
   has no sandbox.
2. **Create the canary:** `echo canary > ~/refdata/CANARY`. Then ask an
   agent in a fresh session to run `just isolation-test`; it must exit 0.
3. **The tool check** from the S0 finding is still open: ask an agent to
   try its Read tool on `~/refdata/CANARY` and its WebFetch tool on
   `openspec.visor.vin`.
