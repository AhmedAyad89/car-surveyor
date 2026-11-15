# Isolation

The project scores itself against OpenSpec. The score means something
only if nothing that builds or runs the pipeline has seen OpenSpec's
data.

## Rules for agents

Copy these to the top of `CLAUDE.md` unchanged.

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

What you may know about OpenSpec is in [sources.md](sources.md) and in
its published schema under `docs/reference_schema/`, which holds field
names and no vehicle data.

## Where things live

| Thing | Place | Read by |
|---|---|---|
| Reference data | `~/refdata/openspec/` | the diff script, when the user runs it |
| Detailed diff output, with reference values | `~/refdata/out/` | the user |
| Feedback file | `data/feedback/<run_id>.json` | agents |

No pipeline table and no dashboard page holds a reference value or the
identity of a style the pipeline missed.

The diff is a deterministic script with no model calls. Agents write and
test it against synthetic fixtures and the schema. It fails when an
agent runs it, because the sandbox blocks `~/refdata/`, so no agent
session ever needs access to the reference. Its error messages name the
file and field, never the value.

## Enforcement

Each control covers a route the others miss.

| Control | Covers |
|---|---|
| Deny rule `Read(~/refdata/**)` | the Read, Grep and Glob tools |
| Sandbox `filesystem.denyRead` | shell subprocesses, such as a Python script that opens the file |
| Deny rules `WebFetch(domain:visor.vin)` and `WebFetch(domain:*.visor.vin)` | fetching; the rule matches the exact host, so subdomains need the wildcard |

Starting point for `.claude/settings.json`. Check the key names against
the Claude Code documentation; the isolation test is what proves it.

```json
{
  "permissions": {
    "deny": [
      "Read(~/refdata/**)",
      "WebFetch(domain:visor.vin)",
      "WebFetch(domain:*.visor.vin)"
    ]
  },
  "sandbox": {
    "enabled": true,
    "allowUnsandboxedCommands": false,
    "filesystem": { "denyRead": ["~/refdata/"] },
    "network": { "deniedDomains": ["visor.vin", "*.visor.vin"] }
  }
}
```

- The sandbox's network allowlist starts empty. Add the hosts the
  pipeline fetches as the probes establish them.
- A deny rule cannot be lifted by another settings file, so there is no
  per-session exception.
- An agent built on the Claude Agent SDK has the sandbox off by default.
  The adjudicator must set the same rules in its own options.
- The pipeline's fetcher also refuses the blocked domains, with a test.

## Feedback contract

The feedback file is the only thing that crosses from the diff to
anything that changes code or data.

| Allowed | Not allowed |
|---|---|
| Metrics per model year and column | reference values |
| Per cell: `agree`, `disagree` or `pipeline_empty` | the reference's value for the cell |
| Per model and model year: counts of styles missed and found extra | the identity of a missed style |
| The bucket assigned after adjudication | anything copied from the reference |

A `disagree` flag says where to look, never what to find.

## Isolation test

- Run by an agent, `just isolation-test` tries to read
  `~/refdata/CANARY` with `cat` and with a Python script, and to reach
  `openspec.visor.vin` with `curl`. All three must fail.
- Run from the user's terminal, the same recipe reads the canary, which
  proves the test is real.
- After any settings change, the user asks an agent to try its Read and
  WebFetch tools on the same targets. Both must be refused.

## Known gaps

- Web search cannot be filtered by domain, so a result can show a
  snippet from a blocked site. Rules 2 and 5 cover it.
- A file attached to a prompt bypasses the tool rules. Do not attach
  reference files.
- The model already knows which Toyota grades exist. That is accepted.
  "Locate, never state" keeps remembered values out.
