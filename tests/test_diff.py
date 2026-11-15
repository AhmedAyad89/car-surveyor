"""Tests for the diff, on synthetic styles and a synthetic reference.

Nothing here is real reference data, and no test reads `~/refdata`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest
from pydantic import ValidationError
from test_reference import EXAMPLE_2023

from car_surveyor import claim, cli, diff, reference, resolution, storage
from car_surveyor.claim import ValueClaim
from car_surveyor.config import Settings
from car_surveyor.diff import DiffResult, Feedback, PipelineCell, PipelineStyle
from car_surveyor.evidence import TableCell
from car_surveyor.reference import ReferenceStyle

NOW = datetime(2026, 11, 16, 10, 12, 33, tzinfo=UTC)


def _cell(*values: float, is_open: bool = False, unit: str = "") -> PipelineCell:
    """A cell whose chosen value is the last one, or none when disagreement left it open."""
    return PipelineCell(chosen=None if is_open else values[-1], values=values, unit=unit)


def _style(description: str, code: str | None = None, **parts: Any) -> PipelineStyle:
    cells = {name: _cell(value) for name, value in parts.pop("values", {}).items()}
    cells.update(parts.pop("cells", {}))
    return PipelineStyle(
        style_key=parts.pop("style_key", f"2023-{code or claim.slugify(description)}"),
        model_year=parts.pop("model_year", 2023),
        model=parts.pop("model", "Example"),
        model_code=code,
        grade=parts.pop("grade", None),
        powertrain=parts.pop("powertrain", None),
        body=parts.pop("body", None),
        engine=parts.pop("engine", None),
        description=description,
        drive_class=parts.pop("drive_class", None),
        transmission_class=parts.pop("transmission_class", None),
        cells=cells,
    )


def _row(trim_name: str, *codes: str, **fields: Any) -> ReferenceStyle:
    trim = claim.slugify(trim_name).replace("-", "_")
    return ReferenceStyle(
        file=fields.pop("file", "example/2023.yaml"),
        model_year=fields.pop("model_year", 2023),
        is_mid_year=fields.pop("is_mid_year", False),
        model_slug=fields.pop("model_slug", "example"),
        model=fields.pop("model", "Example"),
        trim=trim,
        trim_name=trim_name,
        configuration=fields.pop("configuration", "standard"),
        codes=codes,
        labels=fields.pop("labels", {}),
        **fields,
    )


def _build(pipeline: list[PipelineStyle], rows: list[ReferenceStyle], **options: Any) -> DiffResult:
    return diff.build_diff(
        pipeline,
        rows,
        options.pop("adjudications", {}),
        split="dev",
        model_years=(2023,),
        created_at=NOW,
        git_sha="abc1234",
    )


# --- row matching -------------------------------------------------------------


def test_rows_match_by_model_code_whatever_the_names_say() -> None:
    le = _style("Example LE 4-cyl", "9101", grade="LE")
    xle = _style("Example XLE", "9106", grade="XLE")
    rows = [_row("Luxury Edition", "9101"), _row("XLE", "9105", "9106")]
    matches, missed, extra = diff.match_rows([le, xle], rows)
    assert [(found.pipeline.style_key, found.matched_by) for found in matches] == [
        ("2023-9101", "code"),
        ("2023-9106", "code"),
    ]
    assert (missed, extra) == ([], [])


def test_a_code_matches_only_within_its_model_year() -> None:
    style = _style("Example LE", "9101", grade="LE")
    matches, missed, extra = diff.match_rows([style], [_row("LE", "9101", model_year=2022)])
    assert (matches, len(missed), len(extra)) == ([], 1, 1)


def test_a_style_is_matched_by_its_year_file_and_its_mid_year_file() -> None:
    style = _style("Example LE", "9101", grade="LE")
    rows = [_row("LE", "9101"), _row("LE", "9101", file="example/2023_5.yaml", is_mid_year=True)]
    matches, missed, extra = diff.match_rows([style], rows)
    assert [found.reference.file for found in matches] == [
        "example/2023.yaml",
        "example/2023_5.yaml",
    ]
    assert (missed, extra) == ([], [])


def test_a_code_the_document_printed_twice_goes_to_the_row_it_describes() -> None:
    limited = _style("Example 4x2 Limited", "9103", grade="Limited", style_key="2023-limited")
    platinum = _style("Example 4x2 Platinum", "9103", grade="Platinum", style_key="2023-platinum")
    rows = [_row("Platinum", "9103"), _row("Limited", "9104")]
    matches, missed, extra = diff.match_rows([limited, platinum], rows)
    assert [
        (found.reference.trim, found.pipeline.style_key, found.matched_by) for found in matches
    ] == [
        ("platinum", "2023-platinum", "code"),
        ("limited", "2023-limited", "description"),
    ]
    assert (missed, extra) == ([], [])


def test_rows_without_a_code_match_by_description() -> None:
    styles = [
        _style("eX4 XLE FWD", model="eX4", grade="XLE", drive_class="2wd"),
        _style("eX4 XLE AWD", model="eX4", grade="XLE", drive_class="4wd"),
        _style("eX4 Limited FWD", model="eX4", grade="Limited", drive_class="2wd"),
    ]
    rows = [
        _row("XLE", model="eX4", configuration="awd", drivetrain_type="awd"),
        _row("XLE", model="eX4", configuration="fwd", drivetrain_type="fwd"),
        _row("Limited", model="eX4", configuration="awd", drivetrain_type="awd"),
    ]
    matches, missed, extra = diff.match_rows(styles, rows)
    paired = {
        found.reference.configuration + found.reference.trim: found.pipeline.description
        for found in matches
    }
    assert paired == {"awdxle": "eX4 XLE AWD", "fwdxle": "eX4 XLE FWD"}
    assert {found.matched_by for found in matches} == {"description"}
    assert [row.trim for row in missed] == ["limited"]
    assert [style.description for style in extra] == ["eX4 Limited FWD"]


@pytest.mark.parametrize(
    ("style", "row", "is_matched"),
    [
        (_style("Example LE", grade="LE"), _row("LE"), True),
        (_style("Example LE", grade="LE"), _row("XLE"), False),
        (_style("Example LE", grade="LE"), _row("LE", model="Sample"), False),
        (_style("Example LE", grade="LE", drive_class="2wd"), _row("LE", drivetrain_type="awd"), False),
        (_style("Example LE", grade="LE", drive_class="2wd"), _row("LE", labels={"drivetrain": "4WD"}), False),
        (_style("Example Base MT", grade="Base", transmission_class="manual"), _row("Base", transmission_type="automatic"), False),
        (_style("Example Base MT", grade="Base", transmission_class="manual"), _row("Base", transmission_type="manual"), True),
        (_style("Example LE Hybrid", grade="LE", powertrain="Hybrid"), _row("LE", engine_type="gas"), False),
        (_style("Example LE Hybrid", grade="LE", powertrain="Hybrid"), _row("LE", engine_type="hybrid"), True),
        (_style("Example LE Hybrid", grade="LE", powertrain="Hybrid"), _row("LE", model="Example Hybrid", engine_type="hybrid"), True),
        (_style("Example LE", grade="LE"), _row("LE", model="Example Hybrid", engine_type="hybrid"), False),
        (_style("Example Prime SE", grade="SE", powertrain="Prime"), _row("SE", engine_type="hybrid"), False),
        (_style("Example Prime SE", grade="SE", powertrain="Prime"), _row("SE", model="Example Prime", engine_type="phev"), True),
        (_style("Example XLE V6", grade="XLE", engine="V6"), _row("XLE", cylinders=4), False),
        (_style("Example Hatchback SE", grade="SE", body="Hatchback"), _row("SE", model="Example Hatchback"), True),
        (_style("Example Cross LE", model="Example Cross", grade="LE"), _row("LE"), False),
        (_style("Example SR5 Double Cab SB", grade="SR5", body="Double Cab SB"), _row("SR5", labels={"cab": "Double Cab", "bed": "5'"}), True),
        (_style("Example SR5 Access Cab LB", grade="SR5", body="Access Cab LB"), _row("SR5", labels={"cab": "Double Cab"}), False),
        (_style("Example TRD Off-Road", grade="TRD Off-Road"), _row("TRD Off Road"), True),
        (_style("Example LTD", grade="LTD"), _row("Limited"), True),
    ],
)  # fmt: skip
def test_a_description_matches_only_where_nothing_contradicts(
    style: PipelineStyle, row: ReferenceStyle, is_matched: bool
) -> None:
    assert (diff.score_description(style, row) >= diff.MIN_DESCRIPTION_SCORE) is is_matched


def test_matching_is_the_same_whatever_order_the_rows_come_in() -> None:
    styles = [_style(f"Example {grade}", grade=grade) for grade in ("LE", "SE", "XLE")]
    rows = [_row(grade) for grade in ("XLE", "LE", "SE")]
    forward = diff.match_rows(styles, rows)[0]
    backward = diff.match_rows(styles[::-1], rows[::-1])[0]
    assert {(found.pipeline.style_key, found.reference.trim) for found in forward} == {
        (found.pipeline.style_key, found.reference.trim) for found in backward
    }
    assert len(forward) == 3


# --- cells --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attribute", "cell", "fields", "status", "bucket"),
    [
        (claim.MSRP, _cell(28495), {"msrp": 28495}, "agree", "confirmed"),
        (claim.MSRP, _cell(28495), {"msrp": 28496}, "disagree", "open"),
        (claim.MSRP, _cell(28495, 28995), {"msrp": 28495}, "agree", "confirmed"),
        (claim.MSRP, _cell(28495, 28995), {"msrp": 28700}, "disagree", "open"),
        (claim.MSRP, None, {"msrp": 28495}, "pipeline_empty", "pipeline_miss"),
        (claim.MSRP, _cell(28495, 28945, is_open=True), {"msrp": 28495}, "pipeline_empty", "pipeline_miss"),
        (claim.MSRP, _cell(28495), {}, None, "gap_filled"),
        (claim.DELIVERY_FEE, _cell(1095), {"destination_charge": 1095}, "agree", "confirmed"),
        (claim.DELIVERY_FEE, _cell(1095), {"destination_charge": 1335}, "disagree", "open"),
        (claim.HORSEPOWER, _cell(203), {"horsepower": 208}, "agree", "confirmed"),
        (claim.HORSEPOWER, _cell(203), {"horsepower": 209}, "disagree", "open"),
        (claim.TORQUE, _cell(184), {"torque": 179}, "agree", "confirmed"),
        (claim.TORQUE, _cell(184), {"torque": 190}, "disagree", "open"),
        (claim.MPG_CITY, _cell(28), {"mpg_city": 29}, "agree", "confirmed"),
        (claim.MPG_CITY, _cell(28), {"mpg_city": 30}, "disagree", "open"),
        (claim.MPG_CITY, _cell(131, unit="MPGe"), {"mpge_city": 131, "mpg_city": 50}, "agree", "confirmed"),
        (claim.MPG_CITY, _cell(131, unit="MPGe"), {"mpg_city": 131}, None, "gap_filled"),
        (claim.MPG_HIGHWAY, _cell(39, unit="MPG"), {"mpge_highway": 39}, None, "gap_filled"),
    ],
)  # fmt: skip
def test_compare_cell_applies_the_plans_tolerances(
    attribute: str,
    cell: PipelineCell | None,
    fields: dict[str, int],
    status: str | None,
    bucket: str,
) -> None:
    compared = diff.compare_cell(attribute, cell, _row("LE", **fields), [])
    assert compared is not None
    assert (compared.status, compared.bucket) == (status, bucket)


def test_compare_cell_is_none_when_neither_side_has_a_value() -> None:
    assert diff.compare_cell(claim.TORQUE, None, _row("LE"), []) is None


@pytest.mark.parametrize(
    ("adjudicated", "bucket"),
    [
        ([], "open"),
        ([28495.0], "reference_error"),
        ([28995.0], "pipeline_error"),
        ([28495.0, 28995.0], "real_difference"),
        ([27000.0], "both_wrong"),
    ],
)
def test_a_disagreement_is_named_by_what_a_primary_document_says(
    adjudicated: list[float], bucket: str
) -> None:
    compared = diff.compare_cell(claim.MSRP, _cell(28495), _row("LE", msrp=28995), adjudicated)
    assert compared is not None
    assert (compared.status, compared.bucket) == ("disagree", bucket)


# --- the two output files -----------------------------------------------------

# Values chosen so that any of them appearing in the feedback file is a leak.
SECRET_ROWS = [
    _row("LE", "9101", msrp=27771, horsepower=311, mpg_city=41, destination_charge=1777),
    _row("XLE", "9105", msrp=31250, horsepower=206),
    _row("Zzyzx Edition", "7777", msrp=59993, configuration="qqconfig", labels={"cab": "Qqcab"}),
    _row("LE", "9101", msrp=27772, file="example/2023_5.yaml", is_mid_year=True),
]
PIPELINE = [
    _style("Example LE", "9101", grade="LE", values={"msrp": 28495, "horsepower": 203, "torque": 184}),
    _style("Example XLE", "9105", grade="XLE", values={"msrp": 31250, "horsepower": 206}),
    _style("eX4 XLE FWD", model="eX4", grade="XLE", values={"msrp": 42000}),
]  # fmt: skip


def test_build_diff_counts_rows_cells_and_buckets() -> None:
    feedback = _build(PIPELINE, SECRET_ROWS).feedback
    assert feedback.run_id == "20261116T101233Z-dev"
    assert feedback.row_matching.model_dump() == {
        "reference_rows": 4,
        "matched_by_code": 3,
        "matched_by_description": 0,
        "missed": 1,
        "pipeline_styles": 3,
        "extra": 1,
    }
    assert [count.model_dump() for count in feedback.by_reference_model] == [
        {"model_year": 2023, "model": "example", "rows": 4, "matched": 3, "missed": 1}
    ]
    assert [count.model_dump() for count in feedback.by_pipeline_model] == [
        {"model_year": 2023, "model": "Example", "styles": 2, "matched": 2, "extra": 0},
        {"model_year": 2023, "model": "eX4", "styles": 1, "matched": 0, "extra": 1},
    ]
    metrics = {metric.attribute: metric for metric in feedback.metrics}
    assert (metrics["msrp"].agree, metrics["msrp"].disagree) == (1, 2)
    assert (metrics["horsepower"].agree, metrics["horsepower"].disagree) == (1, 1)
    assert metrics["torque"].gap_filled == 2
    assert (metrics["mpg_city"].pipeline_empty, metrics["delivery_fee"].pipeline_empty) == (1, 1)
    assert feedback.buckets == {
        "confirmed": 2,
        "open": 3,
        "reference_error": 0,
        "pipeline_error": 0,
        "real_difference": 0,
        "both_wrong": 0,
        "gap_filled": 3,
        "pipeline_miss": 2,
        "extra_style": 1,
        "missed_style": 1,
    }
    assert feedback.extra_styles == ["2023-ex4-xle-fwd"]
    cells = {
        (cell.style_key, cell.attribute): (cell.status, cell.bucket) for cell in feedback.cells
    }
    assert cells[("2023-9101", "msrp")] == ("disagree", "open")
    assert cells[("2023-9101", "torque")] == (None, "gap_filled")
    assert cells[("2023-9105", "msrp")] == ("agree", "confirmed")
    assert cells[("2023-9101", "mpg_city")] == ("pipeline_empty", "pipeline_miss")


def test_the_feedback_file_holds_no_reference_value_and_no_missed_style() -> None:
    """The feedback contract: a flag says where to look, never what to find."""
    result = _build(PIPELINE, SECRET_ROWS)
    feedback_text = json.dumps(result.feedback.model_dump())
    for leaked in ("27771", "27772", "311", "1777", "59993", "7777", "Zzyzx", "zzyzx", "qqconfig", "Qqcab", "2023_5"):  # fmt: skip
        assert leaked not in feedback_text
    for own_value in ("28495", "31250", "42000", "203"):
        assert own_value not in feedback_text

    detail_text = json.dumps(result.detail)
    for shown in ("27771", "59993", "zzyzx_edition", "qqconfig", "28495"):
        assert shown in detail_text


def test_feedback_is_a_closed_schema() -> None:
    feedback = _build(PIPELINE, SECRET_ROWS).feedback.model_dump()
    with pytest.raises(ValidationError):
        Feedback.model_validate({**feedback, "reference_values": {"2023-9101": 27771}})
    cell = {**feedback["cells"][0], "reference_value": 27771}
    with pytest.raises(ValidationError):
        Feedback.model_validate({**feedback, "cells": [cell]})


def test_the_detail_file_shows_both_sides_and_how_each_row_matched() -> None:
    detail = _build(PIPELINE, SECRET_ROWS).detail
    first = detail["matches"][0]
    assert (first["style_key"], first["matched_by"], first["has_other_code"]) == (
        "2023-9101",
        "code",
        False,
    )
    assert first["reference"]["file"] == "example/2023.yaml"
    price = next(cell for cell in first["cells"] if cell["attribute"] == "msrp")
    assert (price["pipeline_value"], price["reference_value"], price["status"]) == (
        28495,
        27771,
        "disagree",
    )
    (missed,) = detail["missed"]
    assert (missed["trim_name"], missed["codes"], missed["msrp"]) == (
        "Zzyzx Edition",
        ["7777"],
        59993,
    )
    assert detail["extra"] == [{"style_key": "2023-ex4-xle-fwd", "description": "eX4 XLE FWD"}]


def test_build_diff_is_deterministic() -> None:
    first = _build(PIPELINE, SECRET_ROWS)
    second = _build(PIPELINE[::-1], SECRET_ROWS[::-1])
    assert first.feedback == second.feedback
    assert first.detail["matches"] == second.detail["matches"]


def test_describe_prints_counts_and_rates_only() -> None:
    printed = _build(PIPELINE, SECRET_ROWS).describe()
    assert "3 matched by model code, 0 by description, 1 missed" in printed
    assert "style recall 75.0%" in printed
    assert "msrp          agree    1  disagree    2" in printed
    for value in ("27771", "28495", "Zzyzx", "9101"):
        assert value not in printed


def test_write_diff_keeps_held_out_feedback_out_of_the_repository(tmp_path: Path) -> None:
    result = _build(PIPELINE, SECRET_ROWS)
    both = diff.write_diff(result, tmp_path / "out", tmp_path / "feedback")
    assert [path.parent.name for path in both] == ["out", "feedback"]
    written = json.loads((tmp_path / "feedback" / "20261116T101233Z-dev.json").read_text())
    assert Feedback.model_validate(written) == result.feedback

    held_out = diff.write_diff(result, tmp_path / "held", None)
    assert [path.parent.name for path in held_out] == ["held"]


# --- reading the pipeline's side ----------------------------------------------


def _add_style(connection: duckdb.DuckDBPyConnection, key: str, year: int, code: str) -> None:
    connection.execute(
        "INSERT INTO style_records VALUES (?, ?, 'Example', ?, 'LE', NULL, NULL, '4-cyl', NULL, "
        "NULL, 'Example LE 4-cyl', '2wd', true, 'automatic', false, 1)",
        [key, year, code],
    )


def _add_value(
    connection: duckdb.DuckDBPyConnection,
    key: str,
    attribute: str,
    number: float,
    *,
    is_chosen: bool = True,
    specificity: int = 0,
    unit: str = "USD",
) -> None:
    connection.execute(
        "INSERT INTO style_values VALUES (?, ?, ?, ?, ?, ?, NULL, 'oem_pricing', 'sha', ?, '', ?, "
        "false)",
        [key, attribute, f"{key}-{attribute}-{number:g}", number, f"{number:g}", unit, specificity, is_chosen],
    )  # fmt: skip


def test_load_pipeline_styles_reads_the_chosen_value_and_the_dated_ones(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    _add_style(connection, "2023-9101", 2023, "9101")
    _add_style(connection, "2022-9101", 2022, "9101")
    _add_value(connection, "2023-9101", "msrp", 28495, is_chosen=False)
    _add_value(connection, "2023-9101", "msrp", 28995)
    _add_value(connection, "2023-9101", "delivery_fee", 1335, is_chosen=False)
    _add_value(connection, "2023-9101", "delivery_fee", 1095, specificity=2)
    _add_value(connection, "2023-9101", "mpg_city", 32, is_chosen=False, unit="MPG")
    _add_value(connection, "2023-9101", "mpg_city", 31, is_chosen=False, unit="MPG")

    (style,) = diff.load_pipeline_styles(connection, (2023,))

    assert (style.style_key, style.model_code, style.grade, style.drive_class) == (
        "2023-9101",
        "9101",
        "LE",
        "2wd",
    )
    assert style.cells["msrp"] == PipelineCell(28995, (28495, 28995), "USD")
    assert style.cells["delivery_fee"] == PipelineCell(1095, (1095,), "USD")
    assert style.cells["mpg_city"] == PipelineCell(None, (31, 32), "MPG")
    assert "horsepower" not in style.cells


def test_an_adjudication_claim_reaches_the_diff_and_never_a_style(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    judged = ValueClaim(
        claim_id="adjudicated",
        style_key="2023-9101",
        applies_to={},
        attribute=claim.MSRP,
        value_text="$28,495",
        value_num=28495.0,
        unit="USD",
        snapshot_sha256="sha",
        locator=TableCell(1, 0, "9101", "MSRP"),
        quote="$28,495",
        extractor_version="adjudicator",
        source_class=claim.ADJUDICATION,
        status="admitted",
    )
    claim.store_value_claims(connection, [judged])
    assert diff.load_adjudications(connection) == {("2023-9101", claim.MSRP): [28495.0]}
    style = resolution.Style(
        style_key="2023-9101",
        model_year=2023,
        model="Example",
        model_code="9101",
        grade="LE",
        powertrain=None,
        body=None,
        engine=None,
        transmission=None,
        drive=None,
        description="Example LE",
        snapshots=("sha",),
    )
    assert resolution.attach_claims([style], [judged]) == []


# --- the command --------------------------------------------------------------


def _write_reference(root: Path) -> Path:
    model_dir = root / "data" / "us" / "toyota" / "example"
    model_dir.mkdir(parents=True)
    (model_dir / "2023.yaml").write_text(EXAMPLE_2023)
    return root


def _arguments(settings: Settings, bundle: Path, detail_dir: Path) -> list[str]:
    return [
        "--data-dir",
        str(settings.data_dir),
        "diff",
        "dev",
        "--reference",
        str(bundle),
        "--detail-dir",
        str(detail_dir),
    ]


def test_the_diff_command_refuses_to_run_in_an_agent_session(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:  # fmt: skip
    monkeypatch.setenv(cli.AGENT_SESSION_VARIABLE, "1")
    bundle = _write_reference(tmp_path / "bundle")
    status = cli.main(_arguments(settings, bundle, tmp_path / "out"))
    assert status == 3
    assert "only the user runs it" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()
    assert not settings.feedback_dir.exists()

    coverage = ["reference-coverage", "--reference", str(bundle)]
    assert cli.main(coverage) == 3
    assert "configurations" not in capsys.readouterr().out


def test_the_diff_command_writes_both_files_from_the_users_terminal(
    settings: Settings,
    connection: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(cli.AGENT_SESSION_VARIABLE, raising=False)
    _add_style(connection, "2023-9101", 2023, "9101")
    _add_value(connection, "2023-9101", "msrp", 28495)
    connection.close()
    bundle = _write_reference(tmp_path / "bundle")

    status = cli.main(_arguments(settings, bundle, tmp_path / "out"))

    printed = capsys.readouterr().out
    assert status == 0
    assert "reference rows 4: 1 matched by model code, 0 by description, 3 missed" in printed
    (feedback_path,) = settings.feedback_dir.glob("*.json")
    feedback = Feedback.model_validate(json.loads(feedback_path.read_text()))
    assert [(cell.attribute, cell.status) for cell in feedback.cells][:2] == [
        ("delivery_fee", "pipeline_empty"),
        ("horsepower", "pipeline_empty"),
    ]
    (detail_path,) = (tmp_path / "out").glob("*.json")
    assert json.loads(detail_path.read_text())["missed"][0]["file"] == "example/2023.yaml"
    assert "31250" not in feedback_path.read_text()
    reopened = storage.connect(settings.database_path, is_read_only=True)
    reopened.close()


def test_the_diff_command_names_a_broken_reference_field_without_its_value(
    settings: Settings,
    connection: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(cli.AGENT_SESSION_VARIABLE, raising=False)
    connection.close()
    bundle = _write_reference(tmp_path / "bundle")
    broken = bundle / "data" / "us" / "toyota" / "example" / "2023.yaml"
    broken.write_text(EXAMPLE_2023.replace("msrp: 31250", "msrp: thirty-one-thousand"))

    assert cli.main(_arguments(settings, bundle, tmp_path / "out")) == 1

    printed = capsys.readouterr().out
    assert "example/2023.yaml" in printed and "msrp" in printed
    assert "thirty-one-thousand" not in printed


def test_reference_coverage_prints_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(cli.AGENT_SESSION_VARIABLE, raising=False)
    bundle = _write_reference(tmp_path / "bundle")
    assert cli.main(["reference-coverage", "--reference", str(bundle)]) == 0
    printed = capsys.readouterr().out
    assert printed.splitlines()[1].split()[:4] == ["2023", "1", "3", "4"]
    assert reference.load_reference(bundle, "Toyota")[0].msrp == 28495
    assert "28495" not in printed
