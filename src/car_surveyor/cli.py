"""Command line for the pipeline stages. The Justfile recipes call this."""

from __future__ import annotations

import argparse
import os
import subprocess
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from car_surveyor import (
    consistency,
    diff,
    discovery,
    extraction,
    reference,
    resolution,
    snapshot,
    source,
    storage,
    verification,
    viewer,
)
from car_surveyor.config import Settings
from car_surveyor.fetcher import Fetcher
from car_surveyor.source import Registry

# Claude Code sets this in every shell an agent runs.
AGENT_SESSION_VARIABLE = "CLAUDECODE"
AGENT_REFUSAL = """\
{command}: refused. This command reads the reference, so only the user runs it,
  from their own terminal and never from an agent session."""

NO_CREDENTIALS = """\
extract: no Claude API credentials were found, so no document was read.
  Set ANTHROPIC_API_KEY in .env, or run `ant auth login`, and run this again.
  To have another model runner read the documents instead:
    just extract --export data/extraction --label <runner-name>
    (answer each request file, then)
    just extract --import data/extraction --label <runner-name>"""


def main(arguments: Sequence[str] | None = None) -> int:
    options = build_parser().parse_args(arguments)
    if options.stage == "check-answer":
        is_usable, summary = extraction.check_answer_file(options.request, options.answer)
        print(summary)
        return 0 if is_usable else 1
    settings = Settings() if options.data_dir is None else Settings(data_dir=options.data_dir)
    if options.stage == "view":
        # Before anything opens the database for writing: the viewer only reads.
        return viewer.serve(settings, port=options.port, diff_detail_path=options.diff_detail)
    registry = source.load_registry(settings.registry_path)
    if options.stage in ("diff", "reference-coverage"):
        return run_reference_command(registry, settings, options)
    chosen_years = getattr(options, "years", None)
    years = tuple(chosen_years) if chosen_years else registry.model_years
    connection = storage.connect(settings.database_path)
    try:
        if options.stage == "discover":
            run_discover(connection, registry, settings, years, options.retry_unavailable)
        elif options.stage == "snapshot":
            run_snapshot(connection, registry, settings, years, options.retry_unavailable)
        elif options.stage == "extract":
            return run_extract(connection, registry, settings, options)
        elif options.stage == "verify":
            run_verify(connection, registry, settings, is_rechecking=options.recheck)
        elif options.stage == "resolve":
            run_resolve(connection)
        elif options.stage == "run":
            return run_all(connection, registry, settings, options.split)
        else:
            candidates = source.list_candidates(connection, model_years=years)
            print_status(candidates)
            if options.missing:
                print_missing(candidates, settings)
    finally:
        connection.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="car-surveyor", description=__doc__)
    parser.add_argument(
        "--data-dir", type=Path, help="where the database and snapshots are; default: data/"
    )
    stages = parser.add_subparsers(dest="stage", required=True)
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--years", type=int, nargs="+", help="model years to work on; default: all in the registry"
    )
    fetching = argparse.ArgumentParser(add_help=False)
    fetching.add_argument(
        "--retry-unavailable",
        action="store_true",
        help="ask again for addresses an earlier run found unavailable",
    )
    stages.add_parser("discover", parents=[shared, fetching], help="record candidate documents")
    stages.add_parser(
        "snapshot", parents=[shared, fetching], help="fetch each candidate document once"
    )
    extract = stages.add_parser("extract", parents=[shared], help="read each snapshot once")
    extract.add_argument(
        "--retry-failed", action="store_true", help="read again the documents that failed"
    )
    exchange = extract.add_mutually_exclusive_group()
    exchange.add_argument(
        "--export", type=Path, metavar="DIR", help="write request files instead of calling the API"
    )
    exchange.add_argument(
        "--import", type=Path, metavar="DIR", dest="import_dir", help="read answer files from DIR"
    )
    extract.add_argument("--label", help="the name of the runner that answers exported requests")
    verify = stages.add_parser("verify", help="admit or reject each extracted claim")
    verify.add_argument(
        "--recheck", action="store_true", help="settle every claim again, under the current rules"
    )
    stages.add_parser("resolve", help="build one row per style from the admitted claims")
    run = stages.add_parser("run", help="run every stage in order for a split")
    run.add_argument(
        "split",
        choices=("dev", "heldout", "all"),
        help="dev: the development year; heldout: the other model years; all: both",
    )
    compare = stages.add_parser(
        "diff", help="compare the styles with the reference (run by the user only)"
    )
    compare.add_argument("split", choices=("dev", "heldout"))
    compare.add_argument("--reference", type=Path, required=True, help="the OpenSpec bundle")
    compare.add_argument(
        "--detail-dir", type=Path, required=True, help="where the file with both sides goes"
    )
    coverage = stages.add_parser(
        "reference-coverage", help="count what the reference holds (run by the user only)"
    )
    coverage.add_argument("--reference", type=Path, required=True, help="the OpenSpec bundle")
    check_answer = stages.add_parser(
        "check-answer", help="check an answer file against the schema of its request file"
    )
    check_answer.add_argument("request", type=Path)
    check_answer.add_argument("answer", type=Path)
    status = stages.add_parser(
        "status", parents=[shared], help="count candidates by kind and status"
    )
    status.add_argument(
        "--missing",
        action="store_true",
        help="also list each document that could not be fetched, for downloading by hand",
    )
    view = stages.add_parser(
        "view", help="browse the database, the documents and the diff in a local page"
    )
    view.add_argument(
        "--port",
        type=int,
        default=viewer.DEFAULT_PORT,
        help=f"the port on {viewer.HOST} to serve at; default: {viewer.DEFAULT_PORT}",
    )
    view.add_argument(
        "--diff-detail",
        type=Path,
        metavar="FILE",
        help="the diff's detail file, to show reference values beside the pipeline's; "
        "it has no default and is refused in an agent session",
    )
    return parser


def run_discover(
    connection: duckdb.DuckDBPyConnection,
    registry: Registry,
    settings: Settings,
    years: tuple[int, ...],
    is_retrying_unavailable: bool,
) -> None:
    fetcher = Fetcher(registry, settings.user_agent)
    report = discovery.discover(
        connection,
        registry,
        settings,
        fetcher,
        model_years=years,
        is_retrying_unavailable=is_retrying_unavailable,
    )
    print(f"discover: {report.describe()}; {fetcher.request_count} requests")
    for failure in report.failures:
        print(f"  failed: {failure}")
    print_status(source.list_candidates(connection, model_years=years))


def run_snapshot(
    connection: duckdb.DuckDBPyConnection,
    registry: Registry,
    settings: Settings,
    years: tuple[int, ...],
    is_retrying_unavailable: bool,
) -> None:
    fetcher = Fetcher(registry, settings.user_agent)
    report = snapshot.snapshot_documents(
        connection,
        registry,
        settings,
        fetcher,
        model_years=years,
        is_retrying_unavailable=is_retrying_unavailable,
    )
    print(f"snapshot: {report.describe()}; {fetcher.request_count} requests")
    print_status(source.list_candidates(connection, model_years=years))


def run_extract(
    connection: duckdb.DuckDBPyConnection,
    registry: Registry,
    settings: Settings,
    options: argparse.Namespace,
) -> int:
    parsed = extraction.extract_fuel_economy(connection, settings, registry)
    print(f"extract (fuel economy file): {parsed.describe()}")
    if options.export is not None or options.import_dir is not None:
        if not options.label:
            print("extract: --export and --import need --label")
            return 2
        if options.export is not None:
            written = extraction.export_requests(
                connection, settings, options.label, options.export
            )
            print(f"extract: wrote {len(written)} request files to {options.export}/")
            return 0
        report = extraction.import_answers(connection, settings, options.label, options.import_dir)
        print(f"extract (answers from {options.label}): {report.describe()}")
        return 0
    return run_api_extract(connection, settings, is_retrying_failed=options.retry_failed)


def run_api_extract(
    connection: duckdb.DuckDBPyConnection, settings: Settings, *, is_retrying_failed: bool
) -> int:
    key = settings.anthropic_api_key
    extractor = extraction.AnthropicExtractor(
        settings.extraction_model,
        settings.extraction_effort,
        api_key=key.get_secret_value() if key else None,
    )
    try:
        report = extraction.extract_documents(
            connection, settings, extractor, is_retrying_failed=is_retrying_failed
        )
    except TypeError as error:
        # The SDK raises this, not an API error, when it finds no credentials.
        if "authentication" not in str(error):
            raise
        print(NO_CREDENTIALS)
        return 1
    print(f"extract ({extractor.label}): {report.describe()}")
    return 0


def run_verify(
    connection: duckdb.DuckDBPyConnection,
    registry: Registry,
    settings: Settings,
    *,
    is_rechecking: bool = False,
) -> None:
    if is_rechecking:
        verification.reset_claims(connection)
    print(f"verify: {verification.verify_claims(connection, settings, registry).describe()}")


def run_resolve(connection: duckdb.DuckDBPyConnection) -> None:
    print(f"resolve: {resolution.resolve(connection).describe()}")
    findings = consistency.run_checks(connection)
    print(f"consistency checks: {consistency.describe_findings(findings)}")


def run_all(
    connection: duckdb.DuckDBPyConnection, registry: Registry, settings: Settings, split: str
) -> int:
    """Run every stage in order for the development year, the held-out years, or both."""
    held_out = tuple(year for year in registry.model_years if year != registry.development_year)
    years = {
        "dev": (registry.development_year,),
        "heldout": held_out,
        "all": registry.model_years,
    }[split]
    run_discover(connection, registry, settings, years, is_retrying_unavailable=False)
    run_snapshot(connection, registry, settings, years, is_retrying_unavailable=False)
    parsed = extraction.extract_fuel_economy(connection, settings, registry)
    print(f"extract (fuel economy file): {parsed.describe()}")
    status = run_api_extract(connection, settings, is_retrying_failed=False)
    if status != 0:
        return status
    run_verify(connection, registry, settings)
    run_resolve(connection)
    return 0


def run_reference_command(
    registry: Registry, settings: Settings, options: argparse.Namespace
) -> int:
    """Run the diff or the coverage count. Both read the reference, so only the user may.

    The sandbox keeps an agent out of the reference. This refusal is a second
    barrier for a session that started without the sandbox.
    """
    if os.environ.get(AGENT_SESSION_VARIABLE):
        print(AGENT_REFUSAL.format(command=options.stage))
        return 3
    try:
        rows = reference.load_reference(options.reference, registry.make)
    except reference.ReferenceFormatError as error:
        print(f"{options.stage}: {error}")
        return 1
    if options.stage == "reference-coverage":
        print(reference.describe_coverage(rows))
        return 0

    is_development = options.split == "dev"
    years = tuple(
        year
        for year in registry.model_years
        if (year == registry.development_year) == is_development
    )
    if not settings.database_path.is_file():
        print(f"diff: no database at {settings.database_path}; run the pipeline first")
        return 1
    connection = storage.connect(settings.database_path, is_read_only=True)
    try:
        result = diff.build_diff(
            diff.load_pipeline_styles(connection, years),
            [row for row in rows if row.model_year in years],
            diff.load_adjudications(connection),
            split=options.split,
            model_years=years,
            created_at=datetime.now(UTC),
            git_sha=get_git_sha(),
        )
    finally:
        connection.close()
    # Feedback from the held-out years must not reach code before S4.
    feedback_dir = settings.feedback_dir if is_development else None
    print(result.describe())
    for path in diff.write_diff(result, options.detail_dir, feedback_dir):
        print(f"  wrote {path}")
    return 0


def get_git_sha() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        )
    except OSError, subprocess.CalledProcessError:
        return "unknown"
    return completed.stdout.strip()


def print_status(candidates: list[source.Candidate]) -> None:
    counts = Counter((candidate.kind, candidate.status) for candidate in candidates)
    for (kind, status), count in sorted(counts.items()):
        print(f"  {kind:<26} {status:<12} {count}")


def print_missing(candidates: list[source.Candidate], settings: Settings) -> None:
    """List the documents no permitted client could fetch, with where to put a copy.

    A file saved from a browser into the inbox under its original name is
    picked up by the next `snapshot` run.
    """
    missing = [
        candidate
        for candidate in candidates
        if candidate.status == "unavailable" and candidate.kind != source.VEHICLE_PAGE
    ]
    print(f"\n{len(missing)} documents to download by hand into {settings.inbox_dir}/:")
    for candidate in sorted(missing, key=lambda held: (held.model_years, held.kind, held.url)):
        years = ", ".join(str(year) for year in candidate.model_years) or "any year"
        print(f"  [{years}] {candidate.kind}: {candidate.url}")
        print(f"      {candidate.detail}")


if __name__ == "__main__":
    raise SystemExit(main())
