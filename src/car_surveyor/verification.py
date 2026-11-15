"""Verification: a claim is admitted only when code finds it in the document.

`evidence.py` says where a quote is. This module adds what makes that place
the right one for the claim: a price must sit under a price header in the row
of its own style, a horsepower figure must be the number the quote gives in
horsepower and not the rpm beside it, and a fuel economy figure must be the
city, highway or combined member of its triple. A claim that fails any rule
is rejected with the reason. Nothing is repaired: a rejected claim stays in
the table, visible, and its value never reaches a style.

Only claims still `extracted` are looked at, so a second run does nothing.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

import duckdb

from car_surveyor import claim, evidence, source
from car_surveyor.claim import ClaimLocator, PageHint, StyleClaim, ValueClaim
from car_surveyor.config import Settings
from car_surveyor.document import Table
from car_surveyor.evidence import FileRow, TableCell
from car_surveyor.snapshot import Snapshot, list_snapshots
from car_surveyor.source import Registry

MPG_ATTRIBUTES = (claim.MPG_CITY, claim.MPG_HIGHWAY, claim.MPG_COMBINED)
FUEL_ECONOMY_COLUMNS = dict(zip(MPG_ATTRIBUTES, ("city08", "highway08", "comb08"), strict=True))

# The units an attribute may be stated in, folded to letters only. A power
# figure in kilowatts or a torque in newton metres is admitted as printed; the
# resolve stage decides which unit a style's value is taken in.
UNITS_BY_ATTRIBUTE = {
    claim.HORSEPOWER: {"hp": "hp", "horsepower": "hp", "kw": "kw", "ps": "ps"},
    claim.TORQUE: {
        "lbft": "lbft",
        "lbsft": "lbft",
        "poundfeet": "lbft",
        "ftlb": "lbft",
        "nm": "nm",
    },
}
MPG_UNITS = {"mpg", "mpge"}
# Words that say which member of a fuel economy statement a number is.
_MPG_LABELS = {
    claim.MPG_CITY: ("city",),
    claim.MPG_HIGHWAY: ("highway", "hwy"),
    claim.MPG_COMBINED: ("combined", "comb"),
}
# Words that weaken a figure. One printed with a figure must be on the claim.
HEDGES = ("up to", "as low as", "starting at", "estimated", "est.", "preliminary", "projected")
# How far past a delivery fee's quote its list of models may run.
_FEE_SCOPE_WINDOW = 600

_PRICE_HEADER_PATTERN = re.compile(r"\b(msrp|price)\b")
_TRIPLE_PATTERN = re.compile(r"(\d+)\s*/\s*(\d+)\s*/\s*(\d+)")
# Numbers, slashes, and words. A word keeps its inner dots and hyphens, so
# `lb.-ft.` is one token.
_TOKEN_PATTERN = re.compile(r"\d[\d,]*(?:\.\d+)?|/|[^\W\d_]+(?:[.\-']+[^\W\d_]+)*")
_WORD_PATTERN = re.compile(r"[^\W_]+")


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether a claim is admitted, why not, and the location code found for it."""

    is_admitted: bool
    reason: str = ""
    locator: ClaimLocator | None = None


@dataclass(slots=True)
class VerificationReport:
    admitted: int = 0
    rejected: int = 0
    reasons: Counter[str] = field(default_factory=Counter)

    def record(self, decision: Decision) -> None:
        if decision.is_admitted:
            self.admitted += 1
        else:
            self.rejected += 1
            self.reasons[decision.reason] += 1

    def describe(self) -> str:
        lines = [f"{self.admitted} admitted, {self.rejected} rejected"]
        lines += [f"  {count:>5}  {reason}" for reason, count in self.reasons.most_common()]
        return "\n".join(lines)


def verify_claims(
    connection: duckdb.DuckDBPyConnection, settings: Settings, registry: Registry
) -> VerificationReport:
    """Settle every claim that is still `extracted`."""
    report = VerificationReport()
    for snapshot in list_snapshots(connection, settings):
        styles = claim.list_style_claims(
            connection, status="extracted", snapshot_sha256=snapshot.sha256
        )
        values = claim.list_value_claims(
            connection, status="extracted", snapshot_sha256=snapshot.sha256
        )
        if not styles and not values:
            continue
        if snapshot.kind == source.FUEL_ECONOMY_FILE:
            rows = read_file_rows(snapshot)
            for value in values:
                locator = value.locator
                row = rows.get(locator.row_key) if isinstance(locator, FileRow) else None
                decision = check_fuel_economy_claim(value, row, registry.make)
                _settle(connection, report, "value_claims", value.claim_id, decision)
            continue

        text = snapshot.read_text()
        tables = snapshot.read_tables()
        admitted_rows: set[tuple[int, int, str]] = {
            (held.locator.page, held.locator.table, held.locator.row_label)
            for held in claim.list_style_claims(
                connection, status="admitted", snapshot_sha256=snapshot.sha256
            )
            if isinstance(held.locator, TableCell)
        }
        for style in styles:
            decision = check_style_claim(style, tables)
            if decision.is_admitted and isinstance(style.locator, TableCell):
                where = style.locator
                admitted_rows.add((where.page, where.table, where.row_label))
            _settle(connection, report, "style_claims", style.claim_id, decision)
        for value in values:
            if value.attribute == claim.MSRP:
                decision = check_price_claim(value, tables, admitted_rows)
            elif value.attribute == claim.DELIVERY_FEE:
                decision = check_fee_claim(value, text)
            else:
                decision = check_specification_claim(value, text, tables)
            _settle(connection, report, "value_claims", value.claim_id, decision)
    return report


def reset_claims(connection: duckdb.DuckDBPyConnection) -> None:
    """Send every claim back to `extracted`, for a run under changed rules."""
    for table in ("style_claims", "value_claims"):
        connection.execute(f"UPDATE {table} SET status = 'extracted', reject_reason = ''")


def read_file_rows(snapshot: Snapshot) -> dict[str, dict[str, str]]:
    """A data file's rows by their `id`."""
    return {row["id"]: row for row in csv.DictReader(io.StringIO(snapshot.read_text()))}


def check_style_claim(style: StyleClaim, tables: Sequence[Table]) -> Decision:
    """A style exists only if a row of a table says so, in that row's own words."""
    if not isinstance(style.locator, TableCell):
        return Decision(False, "a style needs a table-cell locator")
    grounding = evidence.check_cell_grounding(style.quote, style.quote, tables, style.locator)
    if not grounding.is_grounded or grounding.cell is None:
        return Decision(False, grounding.reason)
    cell = grounding.cell
    if evidence.normalize_for_matching(style.description) != evidence.normalize_for_matching(
        cell.text
    ):
        return Decision(False, "the description is not the located cell")
    row = [evidence.normalize_for_matching(text) for text in cell.row]
    if style.model_code and evidence.normalize_for_matching(style.model_code) not in row:
        return Decision(False, "the model code is not in the located row")
    if str(style.model_year) not in row:
        return Decision(False, "the model year is not in the located row")
    printed = set(_list_words(cell.text))
    parts = {
        "model": style.model,
        "powertrain": style.powertrain,
        "grade": style.grade,
        "drive": style.drive,
        "engine": style.engine,
        "transmission": style.transmission,
        "body": style.body,
    }
    for name, part in parts.items():
        if part and not set(_list_words(part)) <= printed:
            return Decision(False, f"the {name} is not in the description as printed")
    return Decision(True)


def check_price_claim(
    value: ValueClaim, tables: Sequence[Table], admitted_rows: set[tuple[int, int, str]]
) -> Decision:
    """A price must sit under a price header, in the row of a style that was admitted."""
    if not isinstance(value.locator, TableCell):
        return Decision(False, "a price needs a table-cell locator")
    if value.value_num is None:
        return Decision(False, "the value is not one number")
    grounding = evidence.check_cell_grounding(value.quote, value.value_text, tables, value.locator)
    if not grounding.is_grounded or grounding.cell is None:
        return Decision(False, grounding.reason)
    cell = grounding.cell
    header = evidence.normalize_for_matching(cell.header[cell.column_index])
    if not _PRICE_HEADER_PATTERN.search(header):
        return Decision(False, "the column header does not name a price")
    if "$" not in cell.text:
        return Decision(False, "the located cell is not a dollar amount")
    where = value.locator
    if (where.page, where.table, where.row_label) not in admitted_rows:
        return Decision(False, "the style in the same row was not admitted")
    return Decision(True)


def check_fee_claim(value: ValueClaim, text: str) -> Decision:
    """A delivery fee must be a dollar amount in its quote, with its models listed after."""
    if value.value_num is None:
        return Decision(False, "the value is not one number")
    page = value.locator.page if isinstance(value.locator, PageHint) else None
    grounding = evidence.check_grounding(value.quote, value.value_text, text, page=page)
    if not grounding.is_grounded or grounding.span is None:
        return Decision(False, grounding.reason)
    if not _is_dollar_amount(value.value_text, value.quote):
        return Decision(False, "the quote does not give the value in dollars")
    span = grounding.span
    scope = evidence.normalize_for_matching(text[span.start : span.end + _FEE_SCOPE_WINDOW])
    vehicle_class = str(value.applies_to.get("vehicle_class", ""))
    if evidence.normalize_for_matching(vehicle_class) not in scope:
        return Decision(False, "the vehicle class is not printed with the fee")
    for model in value.applies_to.get("models", []):
        if evidence.normalize_for_matching(str(model)) not in scope:
            return Decision(False, "a listed model is not printed with the fee")
    return Decision(True, locator=span)


def check_specification_claim(value: ValueClaim, text: str, tables: Sequence[Table]) -> Decision:
    """A figure must be the quantity its quote states, with its hedges recorded."""
    if value.value_num is None:
        return Decision(False, "the value is not one number")
    label = ""
    locator: ClaimLocator | None = None
    if isinstance(value.locator, TableCell):
        grounding = evidence.check_cell_grounding(
            value.quote, value.value_text, tables, value.locator
        )
        if grounding.is_grounded and grounding.cell is not None:
            label = grounding.cell.row[0]
    else:
        page = value.locator.page if isinstance(value.locator, PageHint) else None
        grounding = evidence.check_grounding(value.quote, value.value_text, text, page=page)
        locator = grounding.span
    if not grounding.is_grounded:
        return Decision(False, grounding.reason)
    problem = find_quantity_problem(value.attribute, value.value_text, value.unit, value.quote)
    if problem is not None:
        return Decision(False, problem)
    recorded = evidence.normalize_for_matching(" ".join(value.hedge_flags))
    for hedge in HEDGES:
        if _has_phrase(hedge, f"{label} {value.quote}") and hedge.rstrip(".") not in recorded:
            return Decision(False, f"the claim leaves out the document's hedge {hedge!r}")
    return Decision(True, locator=locator)


def check_fuel_economy_claim(
    value: ValueClaim, row: Mapping[str, str] | None, make: str
) -> Decision:
    """A fuel economy figure must be the right column of a row of the right make and year."""
    if not isinstance(value.locator, FileRow):
        return Decision(False, "a fuel economy figure needs a file-row locator")
    if value.locator.column != FUEL_ECONOMY_COLUMNS.get(value.attribute):
        return Decision(False, "the column is not the attribute's column")
    grounding = evidence.check_row_grounding(value.value_text, row, value.locator)
    if not grounding.is_grounded or row is None:
        return Decision(False, grounding.reason)
    if row.get("make") != make:
        return Decision(False, "the row is for another make")
    if row.get("year") != str(value.applies_to.get("model_year")):
        return Decision(False, "the row is for another model year")
    return Decision(True)


def find_quantity_problem(attribute: str, value: str, unit: str, quote: str) -> str | None:
    """Why a quote does not state the value as the attribute's quantity, or None if it does.

    A quote that holds a number proves little: `203 hp @ 6,600 rpm` holds two.
    The value must be the number the quote gives in the claim's unit, or the
    member of a fuel economy triple that the attribute names.
    """
    number = evidence.parse_number(value)
    if number is None:
        return "the value is not one number"
    folded_unit = _fold_unit(unit)
    if attribute in MPG_ATTRIBUTES:
        if folded_unit not in MPG_UNITS:
            return "the unit does not fit the attribute"
        position = MPG_ATTRIBUTES.index(attribute)
        triples = _TRIPLE_PATTERN.findall(quote)
        if triples:
            if any(evidence.parse_number(triple[position]) == number for triple in triples):
                return None
            return "the value is not that member of the fuel economy triple"
        if _is_number_beside(number, _MPG_LABELS[attribute], quote):
            return None
        return "the quote does not say which fuel economy figure the value is"
    units = UNITS_BY_ATTRIBUTE.get(attribute, {})
    if folded_unit not in units:
        return "the unit does not fit the attribute"
    same_unit = tuple(name for name, canonical in units.items() if canonical == units[folded_unit])
    if _is_number_beside(number, same_unit, quote):
        return None
    return "the quote does not give the value in the claim's unit"


def _is_number_beside(number: object, words: tuple[str, ...], quote: str) -> bool:
    """Whether the number is followed, or directly preceded, by one of the words.

    Up to three other words may come between a number and the unit after it,
    as in `245 combined net horsepower`, but never another number, except
    that numbers joined by slashes share the unit that follows the last one.
    A unit before the number counts only when no number claims it first: in
    `203 hp @ 6,600 rpm` the `hp` belongs to 203, in `Combined net hp 243` it
    belongs to 243.
    """
    tokens = _TOKEN_PATTERN.findall(evidence.normalize_for_matching(quote))
    for index, token in enumerate(tokens):
        if not token[0].isdigit() or evidence.parse_number(token) != number:
            continue
        position = index + 1
        while position + 1 < len(tokens) and tokens[position] == "/":
            if not tokens[position + 1][0].isdigit():
                break
            position += 2
        for following in tokens[position : position + 4]:
            if following[0].isdigit():
                break
            if _fold_unit(following) in words:
                return True
        is_unit_before = index > 0 and _fold_unit(tokens[index - 1]) in words
        if is_unit_before and not (index > 1 and tokens[index - 2][0].isdigit()):
            return True
    return False


def _is_dollar_amount(value: str, quote: str) -> bool:
    number = evidence.parse_number(value)
    amounts = re.findall(r"\$\s?(\d[\d,]*(?:\.\d+)?)", quote)
    return any(evidence.parse_number(amount) == number for amount in amounts)


def _has_phrase(phrase: str, text: str) -> bool:
    pattern = rf"(?<!\w){re.escape(phrase.rstrip('.'))}(?!\w)"
    return re.search(pattern, evidence.normalize_for_matching(text)) is not None


def _fold_unit(unit: str) -> str:
    return re.sub(r"[^a-z]", "", unit.lower())


def _list_words(text: str) -> list[str]:
    return _WORD_PATTERN.findall(evidence.normalize_for_matching(text))


def _settle(
    connection: duckdb.DuckDBPyConnection,
    report: VerificationReport,
    table: Literal["style_claims", "value_claims"],
    claim_id: str,
    decision: Decision,
) -> None:
    report.record(decision)
    claim.settle_claim(
        connection,
        table,
        claim_id,
        "admitted" if decision.is_admitted else "rejected",
        reject_reason=decision.reason,
        locator=decision.locator,
    )
