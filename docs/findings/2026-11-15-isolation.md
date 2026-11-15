# Probe: isolation settings

Run 2026-11-15 with Claude Code 2.1.311 on macOS. A session reads its
sandbox settings when it starts, so `.claude/settings.json` was tested in
two fresh headless sessions (`claude -p`) started in the repository.

## Answers

| Question | Answer | Count |
|---|---|---|
| Is the sandbox on in a new session? | yes | 2 of 2 sessions |
| Is `openspec.visor.vin` refused to curl? | yes | 2 of 2, "host is on the deny list" |
| Does `denyRead` block `cat`, Python and `ls`? | yes | 9 of 9 attempts |
| Does a `Read(...)` deny rule block the Read tool? | yes | 1 of 1 |
| Does a `Read(...)` deny rule alone block shell reads? | yes | 3 of 3 attempts |
| Does `just isolation-test` exit 0 for an agent? | **not yet** | exit 2: no canary |
| Does `uv` run inside the sandbox? | **no** | 0 of 2 invocations started |
| Does `prek` run inside the sandbox? | **no** | 0 of 1 runs |

## Method

- **Read blocking** was tested on throwaway directories outside the
  repository, each holding a canary and each denied in a different form:
  `dir`, `dir/` and `dir/**`. All three forms blocked every attempt with
  "Operation not permitted". An undenied control stayed readable. The same
  script read all of them from an unsandboxed shell.
- **`~/refdata/` itself was not tested**, because it does not exist yet.
  Until `~/refdata/CANARY` exists, `just isolation-test` reports the two
  canary routes as not judged and exits 2.
- **The recipe's three outcomes** were checked against a scratch canary:
  exit 1 when a route is open, exit 0 when all are blocked, exit 2 when the
  canary is missing.
- **Key names** were checked against the Claude Code documentation. Every
  key in the plan's draft exists. `failIfUnavailable` was added so that a
  session which cannot start the sandbox stops instead of running without
  it.

## Open

1. **Tool caches.** Sandboxed commands may write only inside the working
   directory. `uv` fails with "Failed to initialize cache at
   `~/.cache/uv`", so `just check` cannot run in an agent session, and
   `prek` fails opening `~/.cache/prek/prek.log`, so the commit hook fails
   too. The documented remedy is
   `sandbox.filesystem.allowWrite: ["~/.cache/uv", "~/.cache/prek"]`. It
   widens the sandbox, so it is the user's decision and is not applied.
2. **The canary.** The user creates `~/refdata/CANARY`, then an agent runs
   `just isolation-test` and must see exit 0.
3. **The tool check.** The user asks an agent to try its Read tool on
   `~/refdata/CANARY` and its WebFetch tool on `openspec.visor.vin`. Both
   must be refused. Only the Read rule's form has been proven, on a scratch
   path.
4. **Network allowlist.** It is empty. Hosts the probes reached are named
   in the other two findings. Installing a dependency or pushing from an
   agent session will also ask for `pypi.org`, `files.pythonhosted.org`
   and `github.com`.
5. **Archived copies of the reference.** The deny rules name `visor.vin`.
   They do not match a Wayback Machine copy of a `visor.vin` page, which
   sits at a `web.archive.org` address. Rules 2 and 5 cover it for agents;
   nothing enforces it. See the pressroom finding.

## Incident: reference file names seen during bootstrap

On 2026-11-15 the bootstrap session listed the repository's untracked
`docs/` folder before it had read the isolation rules. The folder held a
copy of the OpenSpec bundle, `docs/openspec-2026-11-09/`. The listing
showed 100 file paths: licence files, schema file names and model-year file
names for one make, which was not Toyota. No file was opened and no value
was seen. The bundle was not copied, moved or committed.

The same session was not sandboxed, having started before the settings
existed, so its one run of `just isolation-test` reached
`openspec.visor.vin` with a HEAD request whose response was discarded.

The bundle has to leave the repository for `~/refdata/openspec/` before
another agent session starts there.
