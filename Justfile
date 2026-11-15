default:
    @just --list

lint:
    uv run ruff check .
    uv run ruff format --check .

fmt:
    uv run ruff check --fix .
    uv run ruff format .

typecheck:
    uv run ty check

test *args:
    uv run pytest {{args}}

check: lint typecheck test

# Tries three routes to the reference: the canary with cat, the canary from a
# Python script, and openspec.visor.vin with curl. Nothing read or fetched is
# printed. Exit 0 means every route was blocked, which is what an agent session
# must see. From your own terminal the routes are open and it exits 1; that
# proves the test is real. Exit 2 means a route could not be judged, usually
# because ~/refdata/CANARY does not exist yet. See docs/plan/isolation.md.
#
# Prove this session cannot reach the reference (exits 0 only if every route is blocked)
isolation-test:
    #!/usr/bin/env bash
    set -uo pipefail
    canary="$HOME/refdata/CANARY"
    host="openspec.visor.vin"
    open_routes=0
    unjudged_routes=0

    blocked() { printf '  %-32s blocked%s\n' "$1" "${2:+ ($2)}"; }
    got_through() { printf '  %-32s GOT THROUGH\n' "$1"; open_routes=$((open_routes + 1)); }
    unjudged() { printf '  %-32s not judged: %s\n' "$1" "$2"; unjudged_routes=$((unjudged_routes + 1)); }

    # A missing canary is told apart from a blocked one: a read that fails
    # because the file is absent says nothing about isolation.
    route="cat ~/refdata/CANARY"
    if error=$(LC_ALL=C cat "$canary" 2>&1 >/dev/null); then
        got_through "$route"
    elif [[ "$error" == *"No such file"* ]]; then
        unjudged "$route" "the canary does not exist"
    else
        blocked "$route"
    fi

    # The system python3, not `uv run`: the route must be judged even where uv
    # cannot start, and the sandbox treats every interpreter alike.
    route="python open(~/refdata/CANARY)"
    outcome=$(python3 - "$canary" <<'PYTHON'
    import sys

    try:
        open(sys.argv[1], "rb").read()
    except FileNotFoundError:
        print("missing")
    except OSError:
        print("blocked")
    else:
        print("read")
    PYTHON
    )
    case "$outcome" in
        read) got_through "$route" ;;
        blocked) blocked "$route" ;;
        missing) unjudged "$route" "the canary does not exist" ;;
        *) unjudged "$route" "python did not run" ;;
    esac

    # A HEAD request with the response discarded: reachability only.
    route="curl https://$host"
    if curl --silent --head --output /dev/null --max-time 20 "https://$host/"; then
        got_through "$route"
    else
        blocked "$route" "curl exit $?"
    fi

    echo
    if [ "$open_routes" -gt 0 ]; then
        echo "NOT ISOLATED: $open_routes of 3 routes to the reference are open."
        echo "Expected from your own terminal. In an agent session, stop and tell the user."
        exit 1
    fi
    if [ "$unjudged_routes" -gt 0 ]; then
        echo "INCONCLUSIVE: $unjudged_routes of 3 routes could not be judged."
        exit 2
    fi
    echo "ISOLATED: all 3 routes to the reference are blocked."
