"""Tests for the resolve stage. The names are made up; the shapes are Toyota's. No network."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from typing import Any

import duckdb
import pytest

from car_surveyor import claim, resolution, storage
from car_surveyor.claim import PageHint, StyleClaim, ValueClaim
from car_surveyor.evidence import FileRow, TableCell
from car_surveyor.resolution import Attachment, Style

PRICING_2022 = "p22" + "0" * 61
PRICING_2023 = "p23" + "0" * 61
SPECIFICATION = "spec" + "0" * 60
FUEL_FILE = "epa" + "0" * 61
DATES: dict[str, tuple[date | None, Any]] = {
    PRICING_2022: (date(2022, 12, 22), datetime(2026, 11, 15, 10)),
    PRICING_2023: (date(2023, 4, 6), datetime(2026, 11, 15, 11)),
    SPECIFICATION: (None, datetime(2026, 11, 15, 12)),
    FUEL_FILE: (None, datetime(2026, 11, 15, 13)),
}


def _style(description: str = "Example LE", **parts: Any) -> Style:
    parts.setdefault("model", "Example")
    parts.setdefault("grade", "LE")
    drive = parts.get("drive")
    transmission = f"{parts.get('transmission') or ''} {parts.get('grade') or ''}"
    return Style(
        style_key=parts.pop("style_key", "2023-" + claim.slugify(description)),
        model_year=parts.pop("model_year", 2023),
        model=parts.pop("model"),
        model_code=parts.pop("model_code", None),
        grade=parts.pop("grade"),
        powertrain=parts.pop("powertrain", None),
        body=parts.pop("body", None),
        engine=parts.pop("engine", None),
        transmission=parts.pop("transmission", None),
        drive=parts.pop("drive", None),
        description=description,
        snapshots=parts.pop("snapshots", (PRICING_2022,)),
        drive_class=parts.pop("drive_class", resolution.get_drive_class(drive)),
        transmission_class=parts.pop(
            "transmission_class", resolution.get_transmission_class(transmission)
        ),
        **parts,
    )


def _style_claim(description: str, snapshot: str = PRICING_2022, **parts: Any) -> StyleClaim:
    parts.setdefault("model", "Example")
    return StyleClaim(
        claim_id=f"{snapshot[:3]}-{claim.slugify(description)}",
        style_key=parts.pop("style_key", "2023-" + claim.slugify(description)),
        model_year=2023,
        model=parts.pop("model"),
        model_code=parts.pop("model_code", None),
        grade=parts.pop("grade", None),
        powertrain=parts.pop("powertrain", None),
        body=parts.pop("body", None),
        engine=parts.pop("engine", None),
        transmission=parts.pop("transmission", None),
        drive=parts.pop("drive", None),
        description=description,
        snapshot_sha256=snapshot,
        locator=TableCell(1, 0, description, "MODEL"),
        quote=description,
        extractor_version="test",
        status="admitted",
    )


def _value(
    attribute: str,
    number: float,
    *,
    claim_id: str = "",
    source_class: str = claim.OEM_SPECIFICATION,
    snapshot: str = SPECIFICATION,
    unit: str = "",
    **changes: Any,
) -> ValueClaim:
    units = {claim.HORSEPOWER: "hp", claim.TORQUE: "lb-ft", claim.MSRP: "USD"}
    value = ValueClaim(
        claim_id=claim_id or f"{attribute}-{number:g}-{snapshot[:4]}",
        style_key=None,
        applies_to={},
        attribute=attribute,
        value_text=f"{number:g}",
        value_num=number,
        unit=unit or units.get(attribute, "MPG"),
        snapshot_sha256=snapshot,
        locator=PageHint(1),
        quote=f"{number:g}",
        extractor_version="test",
        source_class=source_class,
        status="admitted",
    )
    return replace(value, **changes)


def _figure(attribute: str, number: float, **scope: Any) -> ValueClaim:
    """A specification figure of the `2023 Example` document."""
    describes = scope.pop("describes", None)
    unit = scope.pop("unit", "")
    applies_to = {"model_year": 2023, "model": scope.pop("model", "Example"), **scope}
    scope_words = claim.slugify(str(sorted(scope.items())))
    return _value(
        attribute,
        number,
        claim_id=f"{attribute}-{number:g}-{scope_words}-{describes}-{unit}",
        unit=unit,
        applies_to=applies_to,
        qualifiers={"describes": describes} if describes else {},
    )


def _row(row_key: str, name: str, mpg: tuple[int, int, int], **columns: Any) -> list[ValueClaim]:
    """The three claims of one fuel economy file row."""
    scope = {
        "model_year": 2023,
        "model": name,
        "drive": "Front-Wheel Drive",
        "trany": "Automatic (S8)",
        "cylinders": "4",
        "displ": "2.5",
        "atvType": "",
        **columns,
    }
    names = (claim.MPG_CITY, claim.MPG_HIGHWAY, claim.MPG_COMBINED)
    return [
        _value(
            attribute,
            number,
            claim_id=f"{row_key}-{attribute}",
            source_class=claim.EPA,
            snapshot=FUEL_FILE,
            applies_to=scope,
            locator=FileRow(row_key, attribute),
        )
        for attribute, number in zip(names, mpg, strict=True)
    ]


def _chosen(attachments: list[Attachment], style: Style, attribute: str) -> float | None:
    values = {
        attached.value_num
        for attached in attachments
        if (attached.style_key, attached.attribute) == (style.style_key, attribute)
        and attached.is_chosen
    }
    assert len(values) <= 1
    return next(iter(values), None)


def _resolve(styles: list[Style], values: list[ValueClaim]) -> list[Attachment]:
    return resolution.choose_values(resolution.attach_claims(styles, values), DATES)


# --- reading designations -----------------------------------------------------


@pytest.mark.parametrize(
    ("drive", "drive_class"),
    [
        ("4x2", "2wd"),
        ("FWD", "2wd"),
        ("Rear-Wheel Drive", "2wd"),
        ("AWD", "4wd"),
        ("4x4", "4wd"),
        ("Part-time 4-Wheel Drive", "4wd"),
        ("FWD/AWD", None),
        (None, None),
    ],
)
def test_get_drive_class(drive: str | None, drive_class: str | None) -> None:
    assert resolution.get_drive_class(drive) == drive_class


@pytest.mark.parametrize(
    ("text", "transmission_class"),
    [
        ("6MT", "manual"),
        ("MT", "manual"),
        ("3.0 A91-MT", "manual"),
        ("Manual 6-spd", "manual"),
        ("8ECTi", "automatic"),
        ("5ECT", "automatic"),
        ("6A", "automatic"),
        ("CVT", "automatic"),
        ("Automatic (AV-S6)", "automatic"),
        ("Edition A", None),
        ("SE Nightshade", None),
        ("", None),
    ],
)
def test_get_transmission_class(text: str, transmission_class: str | None) -> None:
    assert resolution.get_transmission_class(text) == transmission_class


@pytest.mark.parametrize(
    ("text", "cylinders"),
    [
        ("V6", 6),
        ("4-cyl", 4),
        ("FOUR-CYLINDER", 4),
        ("I4", 4),
        ("XLE V6", 6),
        ("i-FORCE MAX", None),
        ("ENGINE", None),
        ("", None),
    ],
)
def test_get_cylinders(text: str, cylinders: int | None) -> None:
    assert resolution.get_cylinders(text) == cylinders


@pytest.mark.parametrize(
    ("style_grade", "stated", "level"),
    [
        ("LE", "LE", 2),
        ("XLE", "XLE V6", 2),
        ("LE", "LE AWD", 2),
        ("TRD Off-Road", "TRD OFFROAD", 2),
        ("Limited", "LTD", 2),
        ("Platinum", "PLTM", 2),
        ("SE Nightshade", "Nightshade", 1),
        ("Woodland Special Edition", "Woodland Edition", 1),
        ("XLE Premium", "XLE", 1),
        ("XSE", "SE", 0),
        ("LE", "XLE", 0),
        ("Nightshade", "SE Nightshade", 0),
        (None, "LE", 0),
    ],
)
def test_match_grade(style_grade: str | None, stated: str, level: int) -> None:
    assert resolution.match_grade(style_grade, stated) == level


# --- styles -------------------------------------------------------------------


def test_build_styles_merges_documents_and_takes_the_latest_wording() -> None:
    earlier = _style_claim("Example 4x2 LE", style_key="2023-9101", grade="LE", drive="4x2")
    later = _style_claim(
        "Example LE 2WD", PRICING_2023, style_key="2023-9101", grade="LE", drive="2WD"
    )
    (style,) = resolution.build_styles([later, earlier], DATES)
    assert style.description == "Example LE 2WD"
    assert style.snapshots == (PRICING_2023, PRICING_2022)
    assert (style.drive_class, style.is_drive_inferred) == ("2wd", False)


def test_infer_from_siblings_reads_what_the_row_beside_it_implies() -> None:
    styles = resolution.build_styles(
        [
            _style_claim("Example LE", grade="LE"),
            _style_claim("Example LE AWD", grade="LE", drive="AWD"),
            _style_claim("Example XLE", grade="XLE"),
            _style_claim("Example Base", grade="Base"),
            _style_claim("Example Base MT", grade="Base", transmission="MT"),
            _style_claim("Example SR 6AT", grade="SR", transmission="6AT"),
            _style_claim("Example SR V6 6AT", grade="SR", engine="V6", transmission="6AT"),
        ],
        DATES,
    )
    by_description = {style.description: style for style in styles}
    assert by_description["Example LE"].drive_class == "2wd"
    assert by_description["Example LE"].is_drive_inferred
    assert by_description["Example XLE"].drive_class is None
    assert by_description["Example Base"].transmission_class == "automatic"
    assert by_description["Example Base"].is_transmission_inferred
    assert by_description["Example SR 6AT"].excluded_cylinders == {6}
    assert by_description["Example SR V6 6AT"].excluded_cylinders == frozenset()


# --- delivery fees ------------------------------------------------------------


def _fee(models: list[str], snapshot: str = PRICING_2022) -> ValueClaim:
    return _value(
        claim.DELIVERY_FEE,
        1095,
        source_class=claim.OEM_PRICING,
        snapshot=snapshot,
        applies_to={"vehicle_class": "Passenger Cars", "models": models},
    )


@pytest.mark.parametrize(
    ("style", "matched"),
    [
        (_style(), (1, "Example")),
        (_style("Example LE Hybrid", powertrain="Hybrid"), (2, "Example HV")),
        (_style("Example Hatchback SE", body="Hatchback"), (2, "Example Hatchback")),
        (_style("Example SR Double Cab", body="Double Cab"), (1, "Example")),
        (_style("GR Example Core", model="GR Example"), (1, "Example")),
        (_style("GR 9 AT", model="GR9"), (1, "GR 9")),
        (_style("Example Cross LE", model="Example Cross"), None),
        (_style("eX4 XLE", model="eX4"), None),
        (_style(snapshots=(PRICING_2023,)), None),
    ],
)
def test_match_fee_names_a_style_by_the_documents_own_model_list(
    style: Style, matched: tuple[int, str] | None
) -> None:
    fee = _fee(["Example", "Example HV", "Example Hatchback", "GR 9"])
    assert resolution.match_fee(style, fee) == matched


# --- specification documents --------------------------------------------------


@pytest.mark.parametrize(
    ("style", "title", "is_electrified", "covered"),
    [
        (_style(), "Example", False, 1),
        (_style(), "Example Hybrid", True, None),
        (_style("Example LE Hybrid", powertrain="Hybrid"), "Example", False, None),
        (_style("Example LE Hybrid", powertrain="Hybrid"), "Example Hybrid", True, 2),
        (_style("Example LE Hybrid", powertrain="Hybrid"), "Example", True, 1),
        (_style("Example Hatchback SE", body="Hatchback"), "Example", False, None),
        (_style("Example Hatchback SE", body="Hatchback"), "Example Hatchback", False, 2),
        (_style("Example Cross LE", model="Example Cross"), "Example", False, None),
        (_style("Example SR Double Cab", body="Double Cab"), "Example", False, 1),
        (_style("Example Prime SE", powertrain="Prime"), "Example", True, None),
        (_style(model_year=2022), "Example", False, None),
    ],
)
def test_match_document_keeps_a_document_to_its_own_vehicle_line(
    style: Style, title: str, is_electrified: bool, covered: int | None
) -> None:
    assert resolution.match_document(style, 2023, title, is_electrified) == covered


def test_a_figure_for_the_styles_grade_beats_one_for_the_whole_column() -> None:
    xse = _style("Example XSE 4-cyl", grade="XSE", engine="4-cyl")
    le = _style("Example LE 4-cyl", grade="LE", engine="4-cyl")
    v6 = _style("Example XLE V6", grade="XLE", engine="V6")
    figures = [
        _figure(claim.HORSEPOWER, 203, grades=["LE", "SE"], engine="FOUR-CYLINDER"),
        _figure(claim.HORSEPOWER, 206, grades=["XSE"], engine="FOUR-CYLINDER"),
        _figure(claim.HORSEPOWER, 301, grades=[], engine="V6"),
    ]
    resolved = _resolve([xse, le, v6], figures)
    assert _chosen(resolved, xse, claim.HORSEPOWER) == 206
    assert _chosen(resolved, le, claim.HORSEPOWER) == 203
    assert _chosen(resolved, v6, claim.HORSEPOWER) == 301


def test_a_figure_for_the_styles_drive_beats_one_that_names_its_grade_exactly() -> None:
    """Documents list the AWD figures apart and shorten the grade names there."""
    awd = _style("Example SE Nightshade AWD", grade="SE Nightshade", drive="AWD")
    fwd = _style("Example SE Nightshade", grade="SE Nightshade", drive_class="2wd")
    figures = [
        _figure(claim.TORQUE, 184, grades=["LE", "SE Nightshade"]),
        _figure(claim.TORQUE, 182, grades=["LE", "Nightshade"], drive="AWD"),
    ]
    resolved = _resolve([awd, fwd], figures)
    assert _chosen(resolved, awd, claim.TORQUE) == 182
    assert _chosen(resolved, fwd, claim.TORQUE) == 184


def test_a_hybrid_shows_its_system_output_not_its_engine_or_motor() -> None:
    hybrid = _style("Example LE Hybrid", powertrain="Hybrid")
    figures = [
        _figure(claim.HORSEPOWER, 176, model="Example Hybrid", describes="engine"),
        _figure(claim.HORSEPOWER, 208, model="Example Hybrid", describes="Hybrid System Net"),
        _figure(claim.HORSEPOWER, 118, model="Example Hybrid", describes="Electric Motor"),
        _figure(claim.HORSEPOWER, 88, model="Example Hybrid", unit="kW", describes="system"),
        _figure(claim.TORQUE, 163, model="Example Hybrid", describes="engine"),
        _figure(claim.TORQUE, 149, model="Example Hybrid", engine="ELECTRIC MOTOR"),
        _figure(claim.TORQUE, 221, model="Example Hybrid", unit="Nm", describes="engine"),
    ]
    resolved = _resolve([hybrid], figures)
    assert _chosen(resolved, hybrid, claim.HORSEPOWER) == 208
    assert _chosen(resolved, hybrid, claim.TORQUE) == 163
    attached = {a.value_num for a in resolved if a.attribute == claim.HORSEPOWER}
    assert attached == {176, 208}


def test_match_scope_rejects_what_contradicts_the_style() -> None:
    fwd = _style("Example LE", drive_class="2wd", engine="4-cyl")
    assert resolution.match_scope(fwd, {"grades": ["XLE"]}) is None
    assert resolution.match_scope(fwd, {"grades": [], "drive": "AWD"}) is None
    assert resolution.match_scope(fwd, {"grades": [], "engine": "V6"}) is None
    assert resolution.match_scope(fwd, {"grades": [], "transmission": "6MT"}) == (0, "")
    not_v6 = _style("Example SR", grade="SR", excluded_cylinders=frozenset({6}))
    assert resolution.match_scope(not_v6, {"grades": [], "engine": "V6"}) is None
    assert resolution.match_scope(not_v6, {"grades": [], "engine": "FOUR-CYLINDER"}) == (0, "")


def test_match_scope_tells_engines_of_one_family_apart() -> None:
    plain = _style("Example SR5 i-FORCE", grade="SR5", engine="i-FORCE")
    hybrid = _style("Example SR5 i-FORCE MAX", grade="SR5", engine="i-FORCE MAX")
    assert resolution.match_scope(plain, {"grades": [], "engine": "i-FORCE MAX"}) is None
    assert resolution.match_scope(hybrid, {"grades": [], "engine": "i-FORCE MAX"}) == (2, "")
    assert resolution.match_scope(plain, {"grades": [], "engine": "ENGINE"}) == (0, "")


def test_match_scope_does_not_expand_a_range_of_grades() -> None:
    limited = _style("Example Limited", grade="Limited")
    matched = resolution.match_scope(limited, {"grades": ["LE → PLTM"], "drive": "FWD/AWD"})
    assert matched == (0, "grade range not expanded")


# --- the fuel economy file ----------------------------------------------------


FRONT = _style(drive_class="2wd")
ALL_WHEEL = _style("Example LE AWD", drive="AWD")
HATCHBACK = _style("Example Hatchback XSE", grade="XSE", body="Hatchback")
OFF_ROAD = _style("Example TRD Off-Road", grade="TRD Off-Road")
MANUAL_CAB = _style(transmission="6MT", body="Double Cab")
PRIME = _style("Example Prime SE", grade="SE", powertrain="Prime")
AWD_COLUMN = {"drive": "All-Wheel Drive"}
MANUAL_COLUMN = {"trany": "Manual 6-spd"}
PLUG_IN = {"atvType": "Plug-in Hybrid"}
SIX_3_0 = {"cylinders": "6", "displ": "3.0"}


@pytest.mark.parametrize(
    ("style", "name", "columns", "specificity"),
    [
        (FRONT, "Example LE/SE", {}, 3),
        (FRONT, "Example", {}, 1),
        (FRONT, "Example XLE/XSE", {}, None),
        (FRONT, "Example AWD LE/SE", AWD_COLUMN, None),
        (ALL_WHEEL, "Example AWD LE/SE", AWD_COLUMN, 4),
        (_style(), "Example Cross", {}, None),
        (_style("Example Cross LE", model="Example Cross"), "Example", {}, None),
        (_style("GR 9 AT", model="GR9", grade=None), "GR 9", {}, 0),
        (HATCHBACK, "Example Hatchback XSE", {}, 3),
        (OFF_ROAD, "Example TRD OFFROAD", {}, 4),
        (_style(engine="V6"), "Example", {"cylinders": "6"}, 1),
        (_style(engine="V6"), "Example", {"cylinders": "4"}, None),
        (_style(excluded_cylinders=frozenset({6})), "Example", {"cylinders": "6"}, None),
        (_style(grade="3.0 Base", engine="I6"), "Example", SIX_3_0, 2),
        (_style(grade="2.0", engine="I4"), "Example", SIX_3_0, None),
        (_style(transmission="6MT"), "Example", MANUAL_COLUMN, 1),
        (_style(transmission="6MT"), "Example", {}, None),
        (MANUAL_CAB, "Example D-CAB MT", MANUAL_COLUMN, 2),
        (PRIME, "Example Prime SE", PLUG_IN, 3),
        (PRIME, "Example", {}, None),
        (_style(), "Example Prime", PLUG_IN, None),
        (_style(model_year=2022), "Example", {}, None),
    ],
)
def test_match_fuel_economy_row_accounts_for_every_word_of_the_name(
    style: Style, name: str, columns: dict[str, str], specificity: int | None
) -> None:
    scope = _row("1", name, (0, 0, 0), **columns)[0].applies_to
    assert resolution.match_fuel_economy_row(style, scope) == specificity


def test_a_hybrid_never_takes_a_gasoline_row_of_a_model_sold_both_ways() -> None:
    """Even when no hybrid row fits its grade: an empty cell, not a wrong one."""
    nightshade = _style("Example Nightshade Hybrid", grade="Nightshade", powertrain="Hybrid")
    le_hybrid = _style("Example LE Hybrid", powertrain="Hybrid")
    gasoline = _style("Example LE", drive_class="2wd")
    rows = [
        *_row("1", "Example", (22, 33, 26)),
        *_row("2", "Example Hybrid LE", (51, 53, 52), atvType="Hybrid"),
        *_row("3", "Example Hybrid SE/XLE", (44, 47, 46), atvType="Hybrid"),
    ]
    resolved = _resolve([nightshade, le_hybrid, gasoline], rows)
    assert _chosen(resolved, nightshade, claim.MPG_CITY) is None
    assert not [a for a in resolved if a.style_key == nightshade.style_key]
    assert _chosen(resolved, le_hybrid, claim.MPG_CITY) == 51
    assert _chosen(resolved, gasoline, claim.MPG_CITY) == 22


def test_a_model_sold_only_as_a_hybrid_takes_its_hybrid_rows() -> None:
    unmarked = _style("Example LE AWD", drive="AWD")
    rows = _row("1", "Example AWD", (40, 37, 39), atvType="Hybrid", drive="All-Wheel Drive")
    assert _chosen(_resolve([unmarked], rows), unmarked, claim.MPG_COMBINED) == 39


# --- choosing a value ---------------------------------------------------------


def test_two_rows_that_agree_settle_a_cell() -> None:
    style = _style(drive_class="2wd")
    rows = [*_row("1", "Example LE/SE", (28, 39, 32)), *_row("2", "Example LE/SE", (28, 39, 32))]
    resolved = _resolve([style], rows)
    assert _chosen(resolved, style, claim.MPG_HIGHWAY) == 39
    assert not any(attached.is_conflicting for attached in resolved)


def test_two_rows_that_disagree_leave_the_cell_open() -> None:
    style = _style(drive_class="2wd")
    rows = [*_row("1", "Example", (32, 41, 35)), *_row("2", "Example", (31, 40, 34))]
    resolved = _resolve([style], rows)
    assert _chosen(resolved, style, claim.MPG_CITY) is None
    assert all(attached.is_conflicting for attached in resolved)


def test_another_source_that_states_one_of_the_values_settles_the_cell() -> None:
    style = _style(drive_class="2wd")
    rows = [*_row("1", "Example", (32, 41, 35)), *_row("2", "Example", (31, 40, 34))]
    stated = [_figure(claim.MPG_CITY, 32, grades=["LE"])]
    resolved = _resolve([style], rows + stated)
    assert _chosen(resolved, style, claim.MPG_CITY) == 32
    assert _chosen(resolved, style, claim.MPG_HIGHWAY) is None


def test_the_fuel_economy_file_is_preferred_and_the_difference_stays_visible() -> None:
    style = _style(drive_class="2wd")
    values = [*_row("1", "Example", (35, 35, 35)), _figure(claim.MPG_CITY, 36, grades=[])]
    resolved = _resolve([style], values)
    assert _chosen(resolved, style, claim.MPG_CITY) == 35
    city = [attached for attached in resolved if attached.attribute == claim.MPG_CITY]
    assert all(attached.is_conflicting for attached in city)


def test_the_document_is_used_where_the_file_has_no_row() -> None:
    style = _style(drive_class="2wd")
    resolved = _resolve([style], [_figure(claim.MPG_CITY, 36, grades=[])])
    assert _chosen(resolved, style, claim.MPG_CITY) == 36


def _price(number: float, snapshot: str, effective: date | None, style: Style) -> ValueClaim:
    return _value(
        claim.MSRP,
        number,
        source_class=claim.OEM_PRICING,
        snapshot=snapshot,
        style_key=style.style_key,
        effective_date=effective,
    )


def test_the_latest_price_stands_and_the_earlier_one_is_kept_as_history() -> None:
    style = _style(snapshots=(PRICING_2022, PRICING_2023))
    prices = [
        _price(28495, PRICING_2022, date(2022, 12, 22), style),
        _price(28995, PRICING_2023, date(2023, 4, 6), style),
    ]
    resolved = _resolve([style], prices)
    assert _chosen(resolved, style, claim.MSRP) == 28995
    assert sorted(attached.value_num for attached in resolved) == [28495, 28995]
    assert not any(attached.is_conflicting for attached in resolved)


def test_two_prices_of_one_date_are_a_disagreement() -> None:
    style = _style()
    prices = [
        _price(28495, PRICING_2022, date(2022, 12, 22), style),
        replace(_price(28945, PRICING_2022, date(2022, 12, 22), style), claim_id="other"),
    ]
    resolved = _resolve([style], prices)
    assert _chosen(resolved, style, claim.MSRP) is None
    assert all(attached.is_conflicting for attached in resolved)


def test_a_fee_is_taken_from_the_statement_that_names_the_style_most_closely() -> None:
    hatchback = _style("Example Hatchback SE", body="Hatchback")
    close = replace(_fee(["Example Hatchback"]), claim_id="close", value_num=1095.0)
    loose = replace(_fee(["Example"]), claim_id="loose", value_num=1335.0)
    resolved = _resolve([hatchback], [loose, close])
    assert _chosen(resolved, hatchback, claim.DELIVERY_FEE) == 1095


# --- the stage ----------------------------------------------------------------


def _store_snapshot_rows(connection: duckdb.DuckDBPyConnection) -> None:
    for sha256, (document_date, fetched_at) in DATES.items():
        connection.execute(
            "INSERT INTO snapshots VALUES (?, 's', ?, 'u', 'direct', 'k', 'm', 'r', 't', 'j', ?, "
            "NULL, ?)",
            [sha256, f"https://example.test/{sha256}", fetched_at, document_date],
        )


def test_resolve_builds_the_style_table_and_the_views(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    _store_snapshot_rows(connection)
    le = _style_claim("Example LE", grade="LE", style_key="2023-9101", model_code="9101")
    awd = _style_claim("Example LE AWD", grade="LE", drive="AWD", style_key="2023-9102")
    claim.store_style_claims(connection, [le, awd])
    le_style, awd_style = resolution.build_styles([le, awd], DATES)
    claim.store_value_claims(
        connection,
        [
            _price(28495, PRICING_2022, date(2022, 12, 22), le_style),
            _price(29895, PRICING_2022, date(2022, 12, 22), awd_style),
            _fee(["Example"]),
            _figure(claim.HORSEPOWER, 203, grades=["LE"]),
            *_row("1", "Example", (32, 41, 35)),
            *_row("2", "Example", (31, 41, 34)),
            replace(_figure(claim.TORQUE, 999, grades=["LE"]), status="rejected"),
        ],
    )

    report = resolution.resolve(connection)

    assert report.style_count == 2
    assert report.filled[claim.MSRP] == 2
    assert report.conflicting[claim.MPG_CITY] == 1
    (row,) = storage.fetch_records(connection, "SELECT * FROM styles WHERE style_key = '2023-9101'")
    assert (row["msrp"], row["msrp_date"]) == (28495, date(2022, 12, 22))
    assert row["msrp_claim"] == _price(28495, PRICING_2022, None, le_style).claim_id
    assert (row["delivery_fee"], row["horsepower"], row["torque"]) == (1095, 203, None)
    assert (row["mpg_city"], row["mpg_highway"], row["mpg_combined"]) == (None, 41, None)
    assert (row["drive_class"], row["is_drive_inferred"]) == ("2wd", True)

    open_cells = storage.fetch_records(
        connection, "SELECT attribute, stated_values, chosen_value FROM disagreements ORDER BY 1"
    )
    assert open_cells == [
        {"attribute": "mpg_city", "stated_values": [31.0, 32.0], "chosen_value": None},
        {"attribute": "mpg_combined", "stated_values": [34.0, 35.0], "chosen_value": None},
    ]
    coverage = {
        record["attribute"]: record
        for record in storage.fetch_records(connection, "SELECT * FROM coverage")
    }
    assert (coverage["msrp"]["style_count"], coverage["msrp"]["filled_count"]) == (2, 2)
    assert coverage["mpg_city"]["open_count"] == 1
    assert coverage["torque"]["unstated_count"] == 2


def test_resolve_run_twice_gives_the_same_tables(connection: duckdb.DuckDBPyConnection) -> None:
    _store_snapshot_rows(connection)
    le = _style_claim("Example LE", grade="LE")
    claim.store_style_claims(connection, [le])
    (style,) = resolution.build_styles([le], DATES)
    claim.store_value_claims(connection, [_price(28495, PRICING_2022, date(2022, 12, 22), style)])
    resolution.resolve(connection)
    first = storage.fetch_records(connection, "SELECT * FROM style_values ORDER BY ALL")
    resolution.resolve(connection)
    assert storage.fetch_records(connection, "SELECT * FROM style_values ORDER BY ALL") == first
    assert len(first) == 1
