"""The diff: the pipeline's styles set against the reference. Run by the user only.

Deterministic, with no model calls. It reads the resolved styles and the
reference, and writes two files:

- the **detail** file, outside the repository, with both sides' values. It is
  for the user;
- the **feedback** file, under `data/feedback/`, with a status per cell and
  counts. It is the only thing that crosses from the diff to an agent, and it
  holds no reference value and no identity of a style the pipeline missed.
  `Feedback` is a closed schema, so nothing can be added to it by accident.

Row matching, in two passes:

1. **By model code.** A reference configuration carries Toyota's model code,
   and so does a pipeline style. Same model year and same code is a match.
2. **By description**, for reference rows no code matched. The model, drive,
   transmission, powertrain and cylinder count must not contradict, and the
   trim name and cab must share enough words with the style's grade and body.

How many rows each pass matched is reported, because a matching error looks
exactly like a missed style.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import duckdb
from pydantic import BaseModel, ConfigDict

from car_surveyor import claim, resolution, storage
from car_surveyor.reference import ReferenceStyle

# How far a pipeline value may sit from the reference's and still agree.
TOLERANCES = {
    claim.MSRP: 0,
    claim.DELIVERY_FEE: 0,
    claim.HORSEPOWER: 5,
    claim.TORQUE: 5,
    claim.MPG_CITY: 1,
    claim.MPG_HIGHWAY: 1,
    claim.MPG_COMBINED: 1,
}
# The least share of words a trim name and a grade must have in common.
MIN_DESCRIPTION_SCORE = 0.5

CONFIRMED = "confirmed"
OPEN = "open"
REFERENCE_ERROR = "reference_error"
PIPELINE_ERROR = "pipeline_error"
REAL_DIFFERENCE = "real_difference"
BOTH_WRONG = "both_wrong"
GAP_FILLED = "gap_filled"
PIPELINE_MISS = "pipeline_miss"
EXTRA_STYLE = "extra_style"
MISSED_STYLE = "missed_style"
BUCKETS = (
    CONFIRMED,
    OPEN,
    REFERENCE_ERROR,
    PIPELINE_ERROR,
    REAL_DIFFERENCE,
    BOTH_WRONG,
    GAP_FILLED,
    PIPELINE_MISS,
    EXTRA_STYLE,
    MISSED_STYLE,
)

Status = Literal["agree", "disagree", "pipeline_empty"]
# Worst first: what one cell reports when several reference rows match its style.
_STATUS_ORDER: tuple[Status | None, ...] = ("disagree", "pipeline_empty", "agree", None)

_BED_WORDS = frozenset({"bed", "lb", "sb", "ft", "short", "long"})
_BED_LENGTH = re.compile(r"\d+ft")
# Words that say a style's body names a cab.
_CAB_WORDS = frozenset({"cab", "crewmax"})
_REFERENCE_DRIVES = {
    "fwd": resolution.TWO_WHEEL,
    "rwd": resolution.TWO_WHEEL,
    "awd": resolution.FOUR_WHEEL,
    "4wd": resolution.FOUR_WHEEL,
    "4wd_full_time": resolution.FOUR_WHEEL,
}


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RowMatching(_Closed):
    reference_rows: int
    matched_by_code: int
    matched_by_description: int
    missed: int
    pipeline_styles: int
    extra: int


class ReferenceModelCount(_Closed):
    model_year: int
    model: str
    rows: int
    matched: int
    missed: int


class PipelineModelCount(_Closed):
    model_year: int
    model: str
    styles: int
    matched: int
    extra: int


class Metric(_Closed):
    model_year: int
    attribute: str
    agree: int
    disagree: int
    pipeline_empty: int
    gap_filled: int


class CellFeedback(_Closed):
    style_key: str
    attribute: str
    status: Status | None
    bucket: str


class Feedback(_Closed):
    """Everything the diff may tell an agent. See docs/plan/isolation.md."""

    run_id: str
    created_at: str
    split: str
    model_years: list[int]
    git_sha: str
    tolerances: dict[str, int]
    row_matching: RowMatching
    by_reference_model: list[ReferenceModelCount]
    by_pipeline_model: list[PipelineModelCount]
    metrics: list[Metric]
    buckets: dict[str, int]
    cells: list[CellFeedback]
    extra_styles: list[str]


@dataclass(frozen=True, slots=True)
class PipelineCell:
    chosen: float | None
    # Every dated value the style has had. A price keeps its history.
    values: tuple[float, ...]
    unit: str


@dataclass(frozen=True, slots=True)
class PipelineStyle:
    style_key: str
    model_year: int
    model: str
    model_code: str | None
    grade: str | None
    powertrain: str | None
    body: str | None
    engine: str | None
    description: str
    drive_class: str | None
    transmission_class: str | None
    cells: dict[str, PipelineCell]

    @property
    def is_electrified(self) -> bool:
        words = set(resolution.list_words(f"{self.powertrain or ''} {self.engine or ''}"))
        return bool(words & {"hybrid", "prime", "max"})

    @property
    def is_plug_in(self) -> bool:
        return "prime" in resolution.list_words(self.powertrain or "")


@dataclass(frozen=True, slots=True)
class Match:
    pipeline: PipelineStyle
    reference: ReferenceStyle
    matched_by: Literal["code", "description"]
    score: float


@dataclass(frozen=True, slots=True)
class CellComparison:
    attribute: str
    status: Status | None
    bucket: str
    pipeline_value: float | None
    pipeline_values: tuple[float, ...]
    reference_value: int | None


@dataclass(frozen=True, slots=True)
class DiffResult:
    feedback: Feedback
    detail: dict[str, Any]

    def describe(self) -> str:
        """Counts and rates for the terminal. No value from either side."""
        rows = self.feedback.row_matching
        found = rows.matched_by_code + rows.matched_by_description
        recall = found / rows.reference_rows if rows.reference_rows else 0.0
        lines = [
            f"diff {self.feedback.run_id}: model years {self.feedback.model_years}",
            f"  reference rows {rows.reference_rows}: {rows.matched_by_code} matched by model "
            f"code, {rows.matched_by_description} by description, {rows.missed} missed",
            f"  style recall {recall:.1%}; {rows.extra} of {rows.pipeline_styles} pipeline "
            "styles are in no reference row",
        ]
        totals: dict[str, Counter[str]] = defaultdict(Counter)
        for metric in self.feedback.metrics:
            for name in ("agree", "disagree", "pipeline_empty", "gap_filled"):
                totals[metric.attribute][name] += getattr(metric, name)
        for attribute in TOLERANCES:
            counts = totals[attribute]
            compared = counts["agree"] + counts["disagree"]
            rate = f"{counts['agree'] / compared:.1%}" if compared else "n/a"
            lines.append(
                f"  {attribute:<13} agree {counts['agree']:>4}  disagree {counts['disagree']:>4}  "
                f"pipeline empty {counts['pipeline_empty']:>4}  gap filled "
                f"{counts['gap_filled']:>4}  match {rate}"
            )
        return "\n".join(lines)


def load_pipeline_styles(
    connection: duckdb.DuckDBPyConnection, model_years: tuple[int, ...]
) -> list[PipelineStyle]:
    """The resolved styles of the given model years, with their values."""
    values: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for record in storage.fetch_records(
        connection, "SELECT * FROM style_values ORDER BY style_key, attribute, claim_id"
    ):
        values[record["style_key"]][record["attribute"]].append(record)
    styles: list[PipelineStyle] = []
    for record in storage.fetch_records(
        connection, "SELECT * FROM style_records ORDER BY style_key"
    ):
        if record["model_year"] not in model_years:
            continue
        cells: dict[str, PipelineCell] = {}
        for attribute, attached in values[record["style_key"]].items():
            chosen = [row["value_num"] for row in attached if row["is_chosen"]]
            best = max(row["specificity"] for row in attached)
            dated = sorted({row["value_num"] for row in attached if row["specificity"] == best})
            cells[attribute] = PipelineCell(
                chosen=chosen[0] if chosen else None,
                values=tuple(dated),
                unit=attached[0]["unit"],
            )
        styles.append(
            PipelineStyle(
                style_key=record["style_key"],
                model_year=record["model_year"],
                model=record["model"],
                model_code=record["model_code"],
                grade=record["grade"],
                powertrain=record["powertrain"],
                body=record["body"],
                engine=record["engine"],
                description=record["description"],
                drive_class=record["drive_class"],
                transmission_class=record["transmission_class"],
                cells=cells,
            )
        )
    return styles


def load_adjudications(connection: duckdb.DuckDBPyConnection) -> dict[tuple[str, str], list[float]]:
    """What adjudicators found in primary documents, by style and attribute."""
    found: dict[tuple[str, str], list[float]] = defaultdict(list)
    for value in claim.list_value_claims(connection, status="admitted"):
        if value.source_class != claim.ADJUDICATION or value.style_key is None:
            continue
        if value.value_num is not None:
            found[(value.style_key, value.attribute)].append(value.value_num)
    return found


def match_rows(
    pipeline: list[PipelineStyle], reference: list[ReferenceStyle]
) -> tuple[list[Match], list[ReferenceStyle], list[PipelineStyle]]:
    """Pair each reference row with a pipeline style. Returns matches, missed rows, extra styles.

    A pipeline style may be matched by a model year's file and by its mid-year
    file, so it is used at most once per reference file, not once overall.
    """
    by_code: dict[tuple[int, str], list[PipelineStyle]] = defaultdict(list)
    for style in pipeline:
        if style.model_code:
            by_code[(style.model_year, style.model_code)].append(style)

    matches: list[Match] = []
    used: set[tuple[str, str]] = set()
    unmatched: list[ReferenceStyle] = []
    for row in reference:
        coded = {
            style.style_key: style
            for code in row.codes
            for style in by_code.get((row.model_year, code), [])
            if (row.file, style.style_key) not in used
        }
        if not coded:
            unmatched.append(row)
            continue
        # One code on two pipeline styles: the document printed it twice.
        ranked = sorted(
            coded.values(), key=lambda style: (-score_description(style, row), style.style_key)
        )
        best = ranked[0]
        matches.append(Match(best, row, "code", 1.0))
        used.add((row.file, best.style_key))

    pairs = [
        (score, row, style)
        for row in unmatched
        for style in pipeline
        if style.model_year == row.model_year
        and (score := score_description(style, row)) >= MIN_DESCRIPTION_SCORE
    ]
    pairs.sort(key=lambda pair: (-pair[0], _identify(pair[1]), pair[2].style_key))
    described: set[tuple[str, str, str]] = set()
    for score, row, style in pairs:
        if _identify(row) in described or (row.file, style.style_key) in used:
            continue
        matches.append(Match(style, row, "description", round(score, 3)))
        described.add(_identify(row))
        used.add((row.file, style.style_key))

    missed = [row for row in unmatched if _identify(row) not in described]
    matched_keys = {match.pipeline.style_key for match in matches}
    extra = [style for style in pipeline if style.style_key not in matched_keys]
    return matches, missed, extra


def score_description(style: PipelineStyle, row: ReferenceStyle) -> float:
    """How well a reference row's trim and cab words fit a style: 0 where they contradict."""
    if not _is_same_model(style, row):
        return 0.0
    row_drive = _REFERENCE_DRIVES.get(row.drivetrain_type or "") or resolution.get_drive_class(
        row.labels.get("drivetrain")
    )
    if row_drive and style.drive_class and row_drive != style.drive_class:
        return 0.0
    if row.transmission_type and style.transmission_class:
        is_manual = row.transmission_type == "manual"
        if is_manual != (style.transmission_class == resolution.MANUAL):
            return 0.0
    if row.engine_type in ("gas", "diesel", "hybrid", "phev"):
        if (row.engine_type in ("hybrid", "phev")) != style.is_electrified:
            return 0.0
        if (row.engine_type == "phev") != style.is_plug_in:
            return 0.0
    cylinders = resolution.get_cylinders(f"{style.engine or ''} {style.grade or ''}")
    if row.cylinders and cylinders and row.cylinders != cylinders:
        return 0.0

    seats = row.labels.get("seating_capacity")
    labels = [row.labels.get(axis, "") for axis in ("cab", "tier", "body")]
    row_text = " ".join([row.trim_name, *labels, f"{seats}passenger" if seats else ""])
    model_words = set(resolution.list_words(f"{style.model} {row.model}"))
    row_words = _list_name_words(row_text) - model_words
    style_words = _list_name_words(f"{style.grade or ''} {style.body or ''}") - model_words
    # Two cabs share the word "cab" and are still different trucks.
    cab = _list_name_words(row.labels.get("cab", ""))
    if cab and style_words & _CAB_WORDS and not cab <= style_words:
        return 0.0
    if not row_words and not style_words:
        return 1.0
    return len(row_words & style_words) / len(row_words | style_words)


def compare_cell(
    attribute: str,
    cell: PipelineCell | None,
    row: ReferenceStyle,
    adjudicated: list[float],
) -> CellComparison | None:
    """Set one pipeline cell against the reference's. None when both are empty.

    A price agrees when the reference equals any dated value the style has
    had, since the reference keeps one value and the pipeline keeps history.
    """
    is_gasoline_equivalent = cell is not None and cell.unit.lower() == "mpge"
    reference_value = row.get_value(attribute, is_gasoline_equivalent=is_gasoline_equivalent)
    pipeline_value = cell.chosen if cell is not None else None
    dated = cell.values if cell is not None and pipeline_value is not None else ()
    if pipeline_value is None and reference_value is None:
        return None
    tolerance = TOLERANCES[attribute]
    status: Status | None
    if reference_value is None:
        status, bucket = None, GAP_FILLED
    elif pipeline_value is None:
        status, bucket = "pipeline_empty", PIPELINE_MISS
    else:
        candidates = dated if attribute in (claim.MSRP, claim.DELIVERY_FEE) else (pipeline_value,)
        if any(abs(value - reference_value) <= tolerance for value in candidates):
            status, bucket = "agree", CONFIRMED
        else:
            status = "disagree"
            bucket = assign_bucket(pipeline_value, reference_value, adjudicated, tolerance)
    return CellComparison(attribute, status, bucket, pipeline_value, dated, reference_value)


def assign_bucket(
    pipeline_value: float, reference_value: float, adjudicated: list[float], tolerance: float
) -> str:
    """Name a disagreement by what a primary document says, once someone has looked."""
    if not adjudicated:
        return OPEN
    has_pipeline = any(abs(value - pipeline_value) <= tolerance for value in adjudicated)
    has_reference = any(abs(value - reference_value) <= tolerance for value in adjudicated)
    if has_pipeline and has_reference:
        return REAL_DIFFERENCE
    if has_pipeline:
        return REFERENCE_ERROR
    if has_reference:
        return PIPELINE_ERROR
    return BOTH_WRONG


def build_diff(
    pipeline: list[PipelineStyle],
    reference: list[ReferenceStyle],
    adjudications: dict[tuple[str, str], list[float]],
    *,
    split: str,
    model_years: tuple[int, ...],
    created_at: datetime,
    git_sha: str,
) -> DiffResult:
    """Match the rows, compare the cells, and lay out both output files."""
    matches, missed, extra = match_rows(pipeline, reference)
    run_id = f"{created_at:%Y%m%dT%H%M%SZ}-{split}"

    metrics: dict[tuple[int, str], Counter[str]] = defaultdict(Counter)
    buckets: Counter[str] = Counter({name: 0 for name in BUCKETS})
    cells: dict[tuple[str, str], CellFeedback] = {}
    match_details: list[dict[str, Any]] = []
    matches.sort(key=lambda found: (found.pipeline.style_key, _identify(found.reference)))
    for match in matches:
        style, row = match.pipeline, match.reference
        compared: list[dict[str, Any]] = []
        for attribute in TOLERANCES:
            judged = adjudications.get((style.style_key, attribute), [])
            comparison = compare_cell(attribute, style.cells.get(attribute), row, judged)
            if comparison is None:
                continue
            metrics[(style.model_year, attribute)][comparison.status or GAP_FILLED] += 1
            buckets[comparison.bucket] += 1
            held = cells.get((style.style_key, attribute))
            if held is None or _STATUS_ORDER.index(comparison.status) < _STATUS_ORDER.index(
                held.status
            ):
                cells[(style.style_key, attribute)] = CellFeedback(
                    style_key=style.style_key,
                    attribute=attribute,
                    status=comparison.status,
                    bucket=comparison.bucket,
                )
            compared.append(
                {
                    "attribute": attribute,
                    "status": comparison.status,
                    "bucket": comparison.bucket,
                    "pipeline_value": comparison.pipeline_value,
                    "pipeline_values": list(comparison.pipeline_values),
                    "reference_value": comparison.reference_value,
                }
            )
        match_details.append(
            {
                "style_key": style.style_key,
                "description": style.description,
                "matched_by": match.matched_by,
                "score": match.score,
                "has_other_code": bool(row.codes) and style.model_code not in row.codes,
                "reference": _describe_row(row),
                "cells": compared,
            }
        )
    buckets[EXTRA_STYLE] = len(extra)
    buckets[MISSED_STYLE] = len(missed)

    by_code = sum(1 for match in matches if match.matched_by == "code")
    feedback = Feedback(
        run_id=run_id,
        created_at=f"{created_at:%Y-%m-%dT%H:%M:%SZ}",
        split=split,
        model_years=sorted(model_years),
        git_sha=git_sha,
        tolerances=dict(TOLERANCES),
        row_matching=RowMatching(
            reference_rows=len(reference),
            matched_by_code=by_code,
            matched_by_description=len(matches) - by_code,
            missed=len(missed),
            pipeline_styles=len(pipeline),
            extra=len(extra),
        ),
        by_reference_model=_count_reference_models(reference, missed),
        by_pipeline_model=_count_pipeline_models(pipeline, extra),
        metrics=[
            Metric(
                model_year=year,
                attribute=attribute,
                agree=counts["agree"],
                disagree=counts["disagree"],
                pipeline_empty=counts["pipeline_empty"],
                gap_filled=counts[GAP_FILLED],
            )
            for (year, attribute), counts in sorted(metrics.items())
        ],
        buckets=dict(buckets),
        cells=[cells[key] for key in sorted(cells)],
        extra_styles=sorted(style.style_key for style in extra),
    )
    detail = {
        **feedback.model_dump(),
        "matches": match_details,
        "missed": [
            {
                **_describe_row(row),
                "model_year": row.model_year,
                "labels": row.labels,
                "msrp": row.msrp,
            }
            for row in missed
        ],
        "extra": [
            {"style_key": style.style_key, "description": style.description} for style in extra
        ],
    }
    return DiffResult(feedback, detail)


def write_diff(result: DiffResult, detail_dir: Path, feedback_dir: Path | None) -> list[Path]:
    """Write the detail file, and the feedback file unless the split is held out."""
    written: list[Path] = []
    detail_dir.mkdir(parents=True, exist_ok=True)
    detail_path = detail_dir / f"{result.feedback.run_id}.json"
    detail_path.write_text(_to_json(result.detail), encoding="utf-8")
    written.append(detail_path)
    if feedback_dir is not None:
        feedback_dir.mkdir(parents=True, exist_ok=True)
        feedback_path = feedback_dir / f"{result.feedback.run_id}.json"
        feedback_path.write_text(_to_json(result.feedback.model_dump()), encoding="utf-8")
        written.append(feedback_path)
    return written


def _to_json(content: dict[str, Any]) -> str:
    return json.dumps(content, indent=1, sort_keys=True) + "\n"


def _list_name_words(text: str) -> set[str]:
    """The words that name a trim, a grade or a cab, without the bed length.

    The two sides write a bed differently (`5.5-ft. bed`, `SB`, `5'7"`), so
    comparing those words would only count against a true match.
    """
    words = resolution.list_words(resolution.expand_grade(text))
    return {word for word in words if word not in _BED_WORDS and not _BED_LENGTH.fullmatch(word)}


def _identify(row: ReferenceStyle) -> tuple[str, str, str]:
    return (row.file, row.trim, row.configuration)


def _describe_row(row: ReferenceStyle) -> dict[str, Any]:
    return {
        "file": row.file,
        "model": row.model_slug,
        "trim": row.trim,
        "trim_name": row.trim_name,
        "configuration": row.configuration,
        "codes": list(row.codes),
    }


def _is_same_model(style: PipelineStyle, row: ReferenceStyle) -> bool:
    """Whether a reference file's model is the style's, or its line with a powertrain or body."""
    style_model = resolution.squash(style.model)
    row_model = resolution.squash(row.model)
    if row_model == style_model:
        return True
    line = set(resolution.list_words(f"{style.model} {style.powertrain or ''} {style.body or ''}"))
    added = set(resolution.list_words(row.model)) - set(resolution.list_words(style.model))
    return row_model.startswith(style_model) and bool(added) and added <= line


def _count_reference_models(
    reference: list[ReferenceStyle], missed: list[ReferenceStyle]
) -> list[ReferenceModelCount]:
    rows = Counter((row.model_year, row.model_slug) for row in reference)
    lost = Counter((row.model_year, row.model_slug) for row in missed)
    return [
        ReferenceModelCount(
            model_year=year,
            model=model,
            rows=count,
            matched=count - lost[(year, model)],
            missed=lost[(year, model)],
        )
        for (year, model), count in sorted(rows.items())
    ]


def _count_pipeline_models(
    pipeline: list[PipelineStyle], extra: list[PipelineStyle]
) -> list[PipelineModelCount]:
    styles = Counter((style.model_year, style.model) for style in pipeline)
    unmatched = Counter((style.model_year, style.model) for style in extra)
    return [
        PipelineModelCount(
            model_year=year,
            model=model,
            styles=count,
            matched=count - unmatched[(year, model)],
            extra=unmatched[(year, model)],
        )
        for (year, model), count in sorted(styles.items())
    ]
