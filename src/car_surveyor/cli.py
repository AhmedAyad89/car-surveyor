"""Command line for the pipeline stages. The Justfile recipes call this."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence

from car_surveyor import discovery, snapshot, source, storage
from car_surveyor.config import Settings
from car_surveyor.fetcher import Fetcher


def main(arguments: Sequence[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(arguments)
    settings = Settings()
    registry = source.load_registry(settings.registry_path)
    years = tuple(options.years) if options.years else registry.model_years
    connection = storage.connect(settings.database_path)
    try:
        if options.stage == "discover":
            fetcher = Fetcher(registry, settings.user_agent)
            report = discovery.discover(
                connection,
                registry,
                settings,
                fetcher,
                model_years=years,
                is_retrying_unavailable=options.retry_unavailable,
            )
            print(f"discover: {report.describe()}; {fetcher.request_count} requests")
            for failure in report.failures:
                print(f"  failed: {failure}")
        elif options.stage == "snapshot":
            fetcher = Fetcher(registry, settings.user_agent)
            snapshot_report = snapshot.snapshot_documents(
                connection,
                registry,
                settings,
                fetcher,
                model_years=years,
                is_retrying_unavailable=options.retry_unavailable,
            )
            print(f"snapshot: {snapshot_report.describe()}; {fetcher.request_count} requests")
        print_status(source.list_candidates(connection, model_years=years))
    finally:
        connection.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="car-surveyor", description=__doc__)
    stages = parser.add_subparsers(dest="stage", required=True)
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--years", type=int, nargs="+", help="model years to work on; default: all in the registry"
    )
    shared.add_argument(
        "--retry-unavailable",
        action="store_true",
        help="ask again for addresses an earlier run found unavailable",
    )
    stages.add_parser("discover", parents=[shared], help="record candidate documents")
    stages.add_parser("snapshot", parents=[shared], help="fetch each candidate document once")
    stages.add_parser("status", parents=[shared], help="count candidates by kind and status")
    return parser


def print_status(candidates: list[source.Candidate]) -> None:
    counts = Counter((candidate.kind, candidate.status) for candidate in candidates)
    for (kind, status), count in sorted(counts.items()):
        print(f"  {kind:<26} {status:<12} {count}")


if __name__ == "__main__":
    raise SystemExit(main())
