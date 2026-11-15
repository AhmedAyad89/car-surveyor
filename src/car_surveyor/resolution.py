"""Resolution: from admitted claims to one row per style.

A price claim names its style. Every other claim says what it applies to in
its document's own words: a delivery fee lists models, a horsepower figure
lists grades under an engine's column, a fuel economy row is named
`Camry AWD LE/SE`. This stage decides which styles each such claim covers,
and then which value a style shows for each attribute.

The rules, in the order they act:

1. **A claim must not contradict the style.** A figure stated for AWD is not
   attached to a front-drive style, and one from the V6 column is not
   attached to a four-cylinder style. What the style does not print is
   unknown, and unknown does not contradict.
2. **The most specific claim wins.** A figure that names the style's grade
   beats one given for the whole column.
3. **Agreement decides, disagreement stays open.** If the claims left for a
   cell state one value, that is the cell. If they state several, the cell
   is left empty and listed in `disagreements`, unless another source
   states exactly one of them.

Nothing is invented: a style no claim covers has an empty cell. The output
tables are rebuilt from the claims on every run.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from car_surveyor import claim, storage
from car_surveyor.claim import StyleClaim, ValueClaim
from car_surveyor.evidence import FileRow
from car_surveyor.verification import UNITS_BY_ATTRIBUTE

VIEWS_PATH = Path(__file__).resolve().parents[2] / "sql" / "views.sql"

TWO_WHEEL = "2wd"
FOUR_WHEEL = "4wd"
MANUAL = "manual"
AUTOMATIC = "automatic"

_DRIVE_CLASSES = {
    "4x2": TWO_WHEEL,
    "2wd": TWO_WHEEL,
    "fwd": TWO_WHEEL,
    "rwd": TWO_WHEEL,
    "frontwheeldrive": TWO_WHEEL,
    "rearwheeldrive": TWO_WHEEL,
    "4x4": FOUR_WHEEL,
    "4wd": FOUR_WHEEL,
    "awd": FOUR_WHEEL,
    "allwheeldrive": FOUR_WHEEL,
    "4wheeldrive": FOUR_WHEEL,
    "parttime4wheeldrive": FOUR_WHEEL,
}
# How documents shorten grade names. Used only to compare names, never to
# rewrite what a claim says.
_GRADE_ABBREVIATIONS = {
    "ltd": "limited",
    "pltm": "platinum",
    "plat": "platinum",
    "offroad": "off road",
    # The fuel economy file's `TRD-ORP`, read as the TRD Off-Road grade.
    "trdorp": "trd off road",
    "ns": "nightshade",
    "dcab": "double cab",
}
# A column header that titles a section instead of naming an engine.
_SECTION_WORDS = frozenset({"engine", "electric", "motor", "hybrid", "power", "system"})
# Words that make a different vehicle line when added to a model's name. A
# document about the Corolla says nothing about the Corolla Cross.
_LINE_WORDS = frozenset({"hybrid", "prime", "hatchback", "cross", "gr", "crown", "max"})
# Words in a grade list that mean a span of grades, which cannot be expanded
# without knowing the grade order.
_RANGE_PATTERN = re.compile(r"→|>|\bthrough\b|\bthru\b|\bto\b|\+")
_CYLINDER_PATTERN = re.compile(
    r"\b(?:(v|i)[- ]?(\d{1,2})|(\d{1,2})[- ]?cyl(?:inder)?s?|(four|six|eight)[- ]cylinder)\b"
)
_CYLINDER_WORDS = {"four": 4, "six": 6, "eight": 8}
_DISPLACEMENT_PATTERN = re.compile(r"(?<![\d.])(\d\.\d)(?![\d.])")
_WORD_PATTERN = re.compile(r"[a-z0-9]+")

# Which unit a style's figure is taken in when a document prints two.
_CANONICAL_UNITS = {claim.HORSEPOWER: "hp", claim.TORQUE: "lbft"}
# How a source ranks for an attribute; a lower number is preferred. Fuel
# economy is the EPA's figure, so its file comes before Toyota's document.
_SOURCE_RANK = {claim.EPA: 0, claim.OEM_SPECIFICATION: 1, claim.OEM_PRICING: 1}


@dataclass(frozen=True, slots=True)
class Style:
    """One priced configuration, merged from every document that lists it."""

    style_key: str
    model_year: int
    model: str
    model_code: str | None
    grade: str | None
    powertrain: str | None
    body: str | None
    engine: str | None
    transmission: str | None
    drive: str | None
    description: str
    snapshots: tuple[str, ...]
    drive_class: str | None = None
    is_drive_inferred: bool = False
    transmission_class: str | None = None
    is_transmission_inferred: bool = False
    # Cylinder counts the style is known not to have: see `infer_from_siblings`.
    excluded_cylinders: frozenset[int] = frozenset()

    @property
    def cylinders(self) -> int | None:
        return get_cylinders(f"{self.engine or ''} {self.grade or ''}")

    @property
    def model_words(self) -> frozenset[str]:
        return frozenset(list_words(self.model))

    @property
    def line_words(self) -> frozenset[str]:
        """Every word that could be part of the vehicle line's name."""
        return frozenset(list_words(f"{self.model} {self.powertrain or ''} {self.body or ''}"))

    @property
    def is_electrified(self) -> bool:
        words = set(list_words(f"{self.powertrain or ''} {self.engine or ''}"))
        return bool(words & {"hybrid", "prime", "max"})


@dataclass(frozen=True, slots=True)
class Attachment:
    """One admitted claim attached to one style it applies to."""

    style_key: str
    attribute: str
    claim_id: str
    value_num: float
    value_text: str
    unit: str
    effective_date: date | None
    source_class: str
    snapshot_sha256: str
    specificity: int
    note: str = ""
    is_chosen: bool = False
    is_conflicting: bool = False


@dataclass(slots=True)
class ResolutionReport:
    style_count: int = 0
    attachment_count: int = 0
    filled: dict[str, int] = field(default_factory=dict)
    conflicting: dict[str, int] = field(default_factory=dict)

    def describe(self) -> str:
        lines = [f"{self.style_count} styles, {self.attachment_count} values attached"]
        for attribute in claim.ATTRIBUTES:
            lines.append(
                f"  {attribute:<13} {self.filled.get(attribute, 0):>4} filled"
                f" {self.conflicting.get(attribute, 0):>4} left open by disagreement"
            )
        return "\n".join(lines)


def resolve(connection: duckdb.DuckDBPyConnection) -> ResolutionReport:
    """Rebuild the style and value tables from the admitted claims."""
    dates = {
        record["sha256"]: (record["document_date"], record["fetched_at"])
        for record in storage.fetch_records(
            connection, "SELECT sha256, document_date, fetched_at FROM snapshots"
        )
    }
    styles = build_styles(claim.list_style_claims(connection, status="admitted"), dates)
    values = claim.list_value_claims(connection, status="admitted")
    attachments = attach_claims(styles, values)
    attachments = choose_values(attachments, dates)

    connection.execute("DELETE FROM style_records")
    connection.execute("DELETE FROM style_values")
    if styles:
        connection.executemany(
            "INSERT INTO style_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                [
                    style.style_key,
                    style.model_year,
                    style.model,
                    style.model_code,
                    style.grade,
                    style.powertrain,
                    style.body,
                    style.engine,
                    style.transmission,
                    style.drive,
                    style.description,
                    style.drive_class,
                    style.is_drive_inferred,
                    style.transmission_class,
                    style.is_transmission_inferred,
                    len(style.snapshots),
                ]
                for style in styles
            ],
        )
    if attachments:
        connection.executemany(
            "INSERT INTO style_values VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                [
                    attached.style_key,
                    attached.attribute,
                    attached.claim_id,
                    attached.value_num,
                    attached.value_text,
                    attached.unit,
                    attached.effective_date,
                    attached.source_class,
                    attached.snapshot_sha256,
                    attached.specificity,
                    attached.note,
                    attached.is_chosen,
                    attached.is_conflicting,
                ]
                for attached in attachments
            ],
        )
    connection.execute(VIEWS_PATH.read_text(encoding="utf-8"))

    report = ResolutionReport(len(styles), len(attachments))
    for attribute in claim.ATTRIBUTES:
        cells = {a.style_key for a in attachments if a.attribute == attribute}
        chosen = {a.style_key for a in attachments if a.attribute == attribute and a.is_chosen}
        report.filled[attribute] = len(chosen)
        report.conflicting[attribute] = len(cells - chosen)
    return report


def build_styles(
    style_claims: list[StyleClaim], dates: dict[str, tuple[date | None, Any]]
) -> list[Style]:
    """Merge the claims that share a style key, then infer what siblings imply.

    A style seen in several documents takes its words from the latest one.
    """
    by_key: dict[str, list[StyleClaim]] = defaultdict(list)
    for style_claim in style_claims:
        by_key[style_claim.style_key].append(style_claim)
    styles: list[Style] = []
    for style_key, claims in by_key.items():
        latest = max(claims, key=lambda held: _order_snapshot(held.snapshot_sha256, dates))
        styles.append(
            Style(
                style_key=style_key,
                model_year=latest.model_year,
                model=latest.model,
                model_code=latest.model_code,
                grade=latest.grade,
                powertrain=latest.powertrain,
                body=latest.body,
                engine=latest.engine,
                transmission=latest.transmission,
                drive=latest.drive,
                description=latest.description,
                snapshots=tuple(dict.fromkeys(held.snapshot_sha256 for held in claims)),
                drive_class=get_drive_class(latest.drive),
                transmission_class=get_transmission_class(
                    f"{latest.transmission or ''} {latest.grade or ''}"
                ),
            )
        )
    return infer_from_siblings(styles)


def infer_from_siblings(styles: list[Style]) -> list[Style]:
    """Fill in what a style leaves unsaid from the row printed beside it.

    A pricing table lists `Camry LE` and `Camry LE AWD`: the first is the one
    that is not all-wheel drive. Likewise `3.0 Base` beside `3.0 Base MT` is
    the automatic, and `SR Access Cab 6AT` beside `SR Access Cab V6 6AT` is
    the one that is not a V6. The inference is made only when such a sibling
    exists, and the drive and transmission it gives are marked as inferred.
    """
    groups: dict[tuple[Any, ...], list[Style]] = defaultdict(list)
    for style in styles:
        groups[(style.model_year, squash(style.model), squash(style.powertrain or ""))].append(
            style
        )
    inferred: list[Style] = []
    for style in styles:
        siblings = groups[(style.model_year, squash(style.model), squash(style.powertrain or ""))]
        updated = style
        if style.drive_class is None and any(
            sibling.drive_class == FOUR_WHEEL and _is_same_except_drive(style, sibling)
            for sibling in siblings
        ):
            updated = replace(updated, drive_class=TWO_WHEEL, is_drive_inferred=True)
        if style.transmission_class is None and any(
            sibling.transmission_class == MANUAL and _is_same_except_transmission(style, sibling)
            for sibling in siblings
        ):
            updated = replace(updated, transmission_class=AUTOMATIC, is_transmission_inferred=True)
        if style.cylinders is None:
            excluded = frozenset(
                sibling.cylinders
                for sibling in siblings
                if sibling.cylinders is not None and _is_same_except_engine(style, sibling)
            )
            updated = replace(updated, excluded_cylinders=excluded)
        inferred.append(updated)
    return inferred


def attach_claims(styles: list[Style], values: list[ValueClaim]) -> list[Attachment]:
    """Attach every admitted value claim to the styles it applies to."""
    by_key = {style.style_key: style for style in styles}
    attachments: list[Attachment] = []
    specification: dict[str, list[ValueClaim]] = defaultdict(list)
    file_rows: dict[tuple[str, str], list[ValueClaim]] = defaultdict(list)
    for value in values:
        if value.value_num is None:
            continue
        if value.attribute == claim.MSRP:
            if value.style_key in by_key:
                attachments.append(_attach(by_key[value.style_key], value, 0))
        elif value.attribute == claim.DELIVERY_FEE:
            for style in styles:
                matched = match_fee(style, value)
                if matched is not None:
                    attachments.append(_attach(style, value, matched[0], matched[1]))
        elif value.source_class == claim.EPA:
            row_key = value.locator.row_key if isinstance(value.locator, FileRow) else ""
            file_rows[(value.snapshot_sha256, row_key)].append(value)
        else:
            specification[value.snapshot_sha256].append(value)

    for style in styles:
        attachments.extend(_attach_fuel_economy(style, file_rows))
        attachments.extend(_attach_specification(style, specification))
    return attachments


def choose_values(
    attachments: list[Attachment], dates: dict[str, tuple[date | None, Any]]
) -> list[Attachment]:
    """Mark the value each cell shows, and the cells left open by disagreement."""
    cells: dict[tuple[str, str], list[Attachment]] = defaultdict(list)
    for attached in attachments:
        cells[(attached.style_key, attached.attribute)].append(attached)
    settled: list[Attachment] = []
    for (_, attribute), candidates in cells.items():
        if attribute in (claim.MSRP, claim.DELIVERY_FEE):
            settled.extend(_choose_dated(candidates, dates))
        else:
            settled.extend(_choose_by_agreement(candidates))
    return settled


def match_fee(style: Style, fee: ValueClaim) -> tuple[int, str] | None:
    """How many words of a style's line a fee's list of models names, with the name that did.

    A fee applies only to styles priced in the same document. `HV` in a list
    is the document's own short form of Hybrid. Of several listed names that
    fit, the one that covers most of the line is the style's.
    """
    if fee.snapshot_sha256 not in style.snapshots:
        return None
    named_line = list_words(f"{style.model} {style.powertrain or ''}")
    best: tuple[int, str] | None = None
    for listed in fee.applies_to.get("models", []):
        words = [("hybrid" if word == "hv" else word) for word in list_words(str(listed))]
        if "".join(words) == "".join(named_line):
            # The same name, however it is spaced: `GR 86` for `GR86`.
            covered = len(named_line)
        elif words and set(words) <= style.line_words and style.model_words - {"gr"} <= set(words):
            # A listed name may leave out the body or the performance prefix.
            covered = len(set(words))
        else:
            continue
        if best is None or covered > best[0]:
            best = (covered, str(listed))
    return best


def match_scope(style: Style, scope: dict[str, Any]) -> tuple[int, str] | None:
    """How specifically a document's scope names a style, or None if it contradicts it."""
    specificity = 0
    notes: list[str] = []

    grades = [str(grade) for grade in scope.get("grades") or []]
    engine_words = " ".join(grades) + " " + str(scope.get("engine") or "")
    if grades and not any(_RANGE_PATTERN.search(grade) for grade in grades):
        level = max((match_grade(style.grade, grade) for grade in grades), default=0)
        if level == 0:
            return None
        specificity += 2 * level
    elif grades:
        notes.append("grade range not expanded")

    # A figure stated for the style's own drive outranks one that names its
    # grade more exactly but no drive: documents list the all-wheel-drive
    # figures apart, and shorten grade names when they do.
    stated_drive = get_drive_class(scope.get("drive"))
    if stated_drive is not None:
        if style.drive_class is not None and style.drive_class != stated_drive:
            return None
        specificity += 3 if style.drive_class == stated_drive else 0

    stated_cylinders = get_cylinders(engine_words)
    if stated_cylinders is not None and stated_cylinders in style.excluded_cylinders:
        return None
    if stated_cylinders is not None and style.cylinders is not None:
        if stated_cylinders != style.cylinders:
            return None
        specificity += 2
    else:
        # Two names for engines of one family, such as i-FORCE and i-FORCE MAX,
        # share a word and still differ.
        stated_name = set(list_words(str(scope.get("engine") or ""))) - _SECTION_WORDS
        style_name = set(list_words(style.engine or ""))
        if stated_name & style_name:
            if stated_name != style_name:
                return None
            specificity += 2

    stated_transmission = get_transmission_class(str(scope.get("transmission") or ""))
    if stated_transmission is not None:
        if style.transmission_class is not None and style.transmission_class != stated_transmission:
            return None
        specificity += 2 if style.transmission_class == stated_transmission else 0

    body = str(scope.get("body") or "")
    if body:
        if not set(list_words(body)) <= set(list_words(style.body or "")):
            return None
        specificity += 1
    return specificity, "; ".join(notes)


def match_document(style: Style, model_year: Any, model: str, is_electrified: bool) -> int | None:
    """How many words of a style's line a document's title covers, or None if it is another line.

    The title must name the style's model, must add no word the style lacks,
    and must not skip a word that makes a different line. A hybrid-only model
    is titled without the word Hybrid, so a document that states hybrid
    system figures is taken to cover the hybrid.
    """
    if model_year != style.model_year:
        return None
    words = set(list_words(model))
    if not words or not words <= style.line_words or not style.model_words <= words:
        return None
    skipped = (style.line_words & _LINE_WORDS) - words
    if skipped - ({"hybrid", "max"} if is_electrified else set()):
        return None
    return len(words)


def match_fuel_economy_row(style: Style, scope: dict[str, Any]) -> int | None:
    """How specifically a fuel economy row names a style, or None if it is another vehicle.

    The file has no grade field. A row's `model` string starts with the model
    and may add a drive, a body and a group of grades (`Camry AWD LE/SE`). The
    string must start with the style's model, and every word after it must be
    accounted for by the style: a word the style does not explain means the
    row is about something else.
    """
    qualifiers = split_fuel_economy_name(style, scope)
    if qualifiers is None:
        return None

    specificity = 0
    described = set(
        list_words(expand_grade(f"{style.grade or ''} {style.body or ''} {style.powertrain or ''}"))
    )
    for token in qualifiers:
        folded = squash(token)
        if folded in _DRIVE_CLASSES:
            if style.drive_class is not None and style.drive_class != _DRIVE_CLASSES[folded]:
                return None
            specificity += 1 if style.drive_class == _DRIVE_CLASSES[folded] else 0
        elif folded in ("mt", "at"):
            stated = MANUAL if folded == "mt" else AUTOMATIC
            if style.transmission_class not in (None, stated):
                return None
        elif any(match_grade(style.grade, part) for part in token.split("/") if part):
            specificity += 2
        elif set(list_words(expand_grade(token))) <= described:
            specificity += 1
        else:
            return None

    kind = str(scope.get("atvType") or "")
    if ("prime" in style.line_words) != (kind == "Plug-in Hybrid"):
        return None

    row_class = _DRIVE_CLASSES.get(squash(str(scope.get("drive", ""))))
    if row_class is not None and style.drive_class is not None:
        if row_class != style.drive_class:
            return None
        specificity += 1
    row_transmission = get_transmission_class(str(scope.get("trany", "")))
    if row_transmission is not None and style.transmission_class is not None:
        if row_transmission != style.transmission_class:
            return None
        specificity += 1
    row_cylinders = str(scope.get("cylinders") or "")
    if row_cylinders.isdigit() and int(row_cylinders) in style.excluded_cylinders:
        return None
    if style.cylinders is not None and row_cylinders.isdigit():
        if int(row_cylinders) != style.cylinders:
            return None
        specificity += 1
    displacements = _DISPLACEMENT_PATTERN.findall(f"{style.engine or ''} {style.grade or ''}")
    row_displacement = str(scope.get("displ") or "")
    if displacements and row_displacement:
        if row_displacement not in displacements:
            return None
        specificity += 1
    return specificity


def split_fuel_economy_name(style: Style, scope: dict[str, Any]) -> list[str] | None:
    """The words a fuel economy row adds after the style's model, or None if it is another model."""
    if scope.get("model_year") != style.model_year:
        return None
    tokens = str(scope.get("model", "")).split()
    model = squash(style.model)
    for size in range(1, len(tokens) + 1):
        if squash(" ".join(tokens[:size])) == model:
            return tokens[size:]
    return None


def match_grade(style_grade: str | None, stated: str) -> int:
    """2 when a stated grade is the style's grade, 1 when it is part of it, else 0.

    Engine and drive words inside a stated grade (`XLE V6`, `LE AWD`) are
    scope, not name, and are set aside before comparing.
    """
    if not style_grade:
        return 0
    stated_words = [
        word
        for word in list_words(expand_grade(stated))
        if word not in _DRIVE_CLASSES and get_cylinders(word) is None
    ]
    style_words = list_words(expand_grade(style_grade))
    if not stated_words:
        return 0
    if "".join(stated_words) == "".join(style_words):
        return 2
    return 1 if set(stated_words) <= set(style_words) else 0


def expand_grade(grade: str) -> str:
    """A grade name with the documents' abbreviations spelled out."""
    words = [_GRADE_ABBREVIATIONS.get(word, word) for word in list_words(grade)]
    joined = " ".join(words)
    return _GRADE_ABBREVIATIONS.get(squash(joined), joined)


def get_drive_class(drive: Any) -> str | None:
    """Two-wheel or four-wheel drive, or None when one word does not say."""
    return _DRIVE_CLASSES.get(squash(str(drive or "")))


def get_transmission_class(text: str) -> str | None:
    """Manual or automatic, from designations such as `6MT`, `A91-MT`, `8ECTi`, `6A`, `CVT`."""
    words = _WORD_PATTERN.findall(text.lower())
    if any(word == "manual" or re.fullmatch(r"\d?mt", word) for word in words):
        return MANUAL
    is_automatic = any(
        word in ("automatic", "cvt", "ecvt") or re.fullmatch(r"\d{1,2}a|\d{0,2}(at|ect|ecti)", word)
        for word in words
    )
    return AUTOMATIC if is_automatic else None


def get_cylinders(text: str) -> int | None:
    """The cylinder count a designation states (`V6`, `4-cyl`, `FOUR-CYLINDER`), if any."""
    match = _CYLINDER_PATTERN.search(text.lower())
    if match is None:
        return None
    if match.group(4):
        return _CYLINDER_WORDS[match.group(4)]
    return int(match.group(2) or match.group(3))


def list_words(text: str) -> list[str]:
    """Lowercase words, with hyphens and dots inside a word closed up."""
    return _WORD_PATTERN.findall(re.sub(r"(?<=\w)[-.](?=\w)", "", text.lower()))


def squash(text: str) -> str:
    return "".join(_WORD_PATTERN.findall(text.lower()))


def _attach(style: Style, value: ValueClaim, specificity: int, note: str = "") -> Attachment:
    return Attachment(
        style_key=style.style_key,
        attribute=value.attribute,
        claim_id=value.claim_id,
        value_num=value.value_num if value.value_num is not None else 0.0,
        value_text=value.value_text,
        unit=value.unit,
        effective_date=value.effective_date,
        source_class=value.source_class,
        snapshot_sha256=value.snapshot_sha256,
        specificity=specificity,
        note=note,
    )


def _attach_fuel_economy(
    style: Style, file_rows: dict[tuple[str, str], list[ValueClaim]]
) -> list[Attachment]:
    """Attach the fuel economy rows that name a style.

    One name can cover a hybrid and a non-hybrid (`Tundra 2WD` is both). Where
    the file has rows of both kinds for the style's model, only the kind the
    style is are kept, even if none of that kind fits. Where every row for the
    model is a hybrid, the model is sold only as one and they are kept.
    """
    fitting: list[tuple[int, bool, list[ValueClaim]]] = []
    model_kinds: set[bool] = set()
    for figures in file_rows.values():
        scope = figures[0].applies_to
        if split_fuel_economy_name(style, scope) is None:
            continue
        is_hybrid = str(scope.get("atvType") or "") in ("Hybrid", "Plug-in Hybrid")
        model_kinds.add(is_hybrid)
        specificity = match_fuel_economy_row(style, scope)
        if specificity is not None:
            fitting.append((specificity, is_hybrid, figures))
    if len(model_kinds) > 1:
        fitting = [row for row in fitting if row[1] == style.is_electrified]
    return [
        _attach(style, figure, specificity, str(figure.applies_to.get("model", "")))
        for specificity, _, figures in fitting
        for figure in figures
    ]


def _attach_specification(
    style: Style, by_snapshot: dict[str, list[ValueClaim]]
) -> list[Attachment]:
    """Attach the figures of the documents whose title best covers the style's line."""
    covering: dict[str, int] = {}
    for sha256, figures in by_snapshot.items():
        scope = figures[0].applies_to
        is_electrified = any(_get_role(figure) == "system" for figure in figures)
        covered = match_document(
            style, scope.get("model_year"), str(scope.get("model", "")), is_electrified
        )
        if covered is not None:
            covering[sha256] = covered
    if not covering:
        return []
    best = max(covering.values())
    attachments: list[Attachment] = []
    for sha256, covered in covering.items():
        if covered != best:
            continue
        for figure in by_snapshot[sha256]:
            role = _get_role(figure)
            canonical = _CANONICAL_UNITS.get(figure.attribute)
            unit = UNITS_BY_ATTRIBUTE.get(figure.attribute, {}).get(
                re.sub(r"[^a-z]", "", figure.unit.lower())
            )
            if role == "motor" or (canonical is not None and unit != canonical):
                continue
            matched = match_scope(style, figure.applies_to)
            if matched is None:
                continue
            specificity, note = matched
            # A hybrid's figure is its system output where the document gives one.
            if role == "system":
                specificity += 100
            attachments.append(_attach(style, figure, specificity, note or role))
    return attachments


def _get_role(figure: ValueClaim) -> str:
    """Whether a power or torque figure is the engine's, an electric motor's, or the system's."""
    described = f"{figure.qualifiers.get('describes', '')} {figure.applies_to.get('engine') or ''}"
    words = set(list_words(described))
    if "motor" in words:
        return "motor"
    if words & {"combined", "system", "total"}:
        return "system"
    return "engine"


def _choose_dated(
    candidates: list[Attachment], dates: dict[str, tuple[date | None, Any]]
) -> list[Attachment]:
    """A price is a dated value: the latest stands, and earlier ones are history.

    Among fee statements, the one that names the style most closely is used.
    Two values with the same date and standing are a disagreement.
    """
    best = max(attached.specificity for attached in candidates)
    leading = [attached for attached in candidates if attached.specificity == best]

    def order(attached: Attachment) -> tuple[Any, ...]:
        return (
            attached.effective_date or date.min,
            _order_snapshot(attached.snapshot_sha256, dates),
        )

    newest = max(order(attached) for attached in leading)
    latest = [attached for attached in leading if order(attached) == newest]
    is_conflicting = len({attached.value_num for attached in latest}) > 1
    return [
        replace(
            attached,
            is_chosen=attached in latest and not is_conflicting,
            is_conflicting=attached in latest and is_conflicting,
        )
        for attached in candidates
    ]


def _choose_by_agreement(candidates: list[Attachment]) -> list[Attachment]:
    """Per source, keep the most specific claims; then take the preferred source's value.

    A source whose leading claims disagree settles nothing by itself. If
    another source states exactly one of its values, that value is chosen.
    """
    leading: dict[str, list[Attachment]] = {}
    for source_class in {attached.source_class for attached in candidates}:
        own = [attached for attached in candidates if attached.source_class == source_class]
        best = max(attached.specificity for attached in own)
        leading[source_class] = [attached for attached in own if attached.specificity == best]

    chosen_value: float | None = None
    for source_class in sorted(leading, key=lambda name: _SOURCE_RANK.get(name, 9)):
        values = {attached.value_num for attached in leading[source_class]}
        if len(values) == 1:
            chosen_value = next(iter(values))
            break
        others = {
            attached.value_num
            for other, claims in leading.items()
            if other != source_class
            for attached in claims
        }
        if len(values & others) == 1:
            chosen_value = next(iter(values & others))
            break

    in_lead = {id(attached) for claims in leading.values() for attached in claims}
    lead_values = {attached.value_num for claims in leading.values() for attached in claims}
    return [
        replace(
            attached,
            is_chosen=id(attached) in in_lead and attached.value_num == chosen_value,
            is_conflicting=id(attached) in in_lead and len(lead_values) > 1,
        )
        for attached in candidates
    ]


def _order_snapshot(sha256: str, dates: dict[str, tuple[date | None, Any]]) -> tuple[Any, ...]:
    document_date, fetched_at = dates.get(sha256, (None, None))
    return (document_date or date.min, str(fetched_at or ""), sha256)


def _is_same_except_drive(style: Style, sibling: Style) -> bool:
    return (
        style.style_key != sibling.style_key
        and squash(style.grade or "") == squash(sibling.grade or "")
        and squash(style.body or "") == squash(sibling.body or "")
        and get_cylinders(style.engine or "") == get_cylinders(sibling.engine or "")
    )


def _is_same_except_engine(style: Style, sibling: Style) -> bool:
    return (
        style.style_key != sibling.style_key
        and squash(style.grade or "") == squash(sibling.grade or "")
        and squash(style.body or "") == squash(sibling.body or "")
        and style.drive_class == sibling.drive_class
        and style.transmission_class == sibling.transmission_class
    )


def _is_same_except_transmission(style: Style, sibling: Style) -> bool:
    def name(held: Style) -> str:
        words = [word for word in list_words(held.grade or "") if not re.fullmatch(r"\d?mt", word)]
        return "".join(words)

    return (
        style.style_key != sibling.style_key
        and name(style) == name(sibling)
        and squash(style.body or "") == squash(sibling.body or "")
        and style.drive_class == sibling.drive_class
    )
