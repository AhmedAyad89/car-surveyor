"""Command line for the pipeline stages. The Justfile recipes call this."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import duckdb

from car_surveyor import discovery, extraction, snapshot, source, storage
from car_surveyor.config import Settings
from car_surveyor.fetcher import Fetcher
from car_surveyor.source import Registry

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
    registry = source.load_registry(settings.registry_path)
    years = tuple(options.years) if options.years else registry.model_years
    connection = storage.connect(settings.database_path)
    try:
        if options.stage == "discover":
            run_discover(connection, registry, settings, years, options.retry_unavailable)
        elif options.stage == "snapshot":
            run_snapshot(connection, registry, settings, years, options.retry_unavailable)
        elif options.stage == "extract":
            return run_extract(connection, registry, settings, options)
        else:
            print_status(source.list_candidates(connection, model_years=years))
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
    check_answer = stages.add_parser(
        "check-answer", help="check an answer file against the schema of its request file"
    )
    check_answer.add_argument("request", type=Path)
    check_answer.add_argument("answer", type=Path)
    stages.add_parser("status", parents=[shared], help="count candidates by kind and status")
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

    key = settings.anthropic_api_key
    extractor = extraction.AnthropicExtractor(
        settings.extraction_model,
        settings.extraction_effort,
        api_key=key.get_secret_value() if key else None,
    )
    try:
        report = extraction.extract_documents(
            connection, settings, extractor, is_retrying_failed=options.retry_failed
        )
    except TypeError as error:
        # The SDK raises this, not an API error, when it finds no credentials.
        if "authentication" not in str(error):
            raise
        print(NO_CREDENTIALS)
        return 1
    print(f"extract ({extractor.label}): {report.describe()}")
    return 0


def print_status(candidates: list[source.Candidate]) -> None:
    counts = Counter((candidate.kind, candidate.status) for candidate in candidates)
    for (kind, status), count in sorted(counts.items()):
        print(f"  {kind:<26} {status:<12} {count}")


if __name__ == "__main__":
    raise SystemExit(main())
