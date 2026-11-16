"""Extraction: one model call per document, and a parser for the fuel economy file.

The model is asked where things are, not what they are worth. Its answer names
a table, a row label and a column header for every price, and a quote for
every figure; the verify stage then reads the document at those places. An
answer is kept as it was returned, keyed on the snapshot and the extractor
version, so a second run finds it and makes no call.

Two ways to get an answer:

- `AnthropicExtractor` calls the Claude API with a structured output schema.
- `export_requests` writes each pending request to a file and
  `import_responses` reads answers back, for a model that is not reached
  through the API. Either way the answer passes the same schema and the same
  verification.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

import anthropic
import duckdb
from pydantic import BaseModel, ConfigDict, ValidationError

from car_surveyor import claim, document, evidence, source
from car_surveyor.claim import PageHint, StyleClaim, ValueClaim
from car_surveyor.config import Settings
from car_surveyor.evidence import FileRow, TableCell
from car_surveyor.snapshot import Snapshot, list_snapshots
from car_surveyor.source import Registry

PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"
# The prompt each kind of document is read with. A changed prompt is a new
# file and a new name, so its answers never mix with the old prompt's.
PROMPTS_BY_KIND = {
    source.PRICING_PDF: "pricing-v1",
    source.PRODUCT_INFORMATION_PDF: "specification-v1",
}
FUEL_ECONOMY_PARSER = "vehicles-csv-v1"
# The kinds the API reads unless told otherwise. Haiku without thinking read
# price sheets as well as Claude Code Opus, and located spec sheet figures too
# poorly to admit (docs/findings/2026-11-16-haiku-vs-opus-extraction.md), so
# spec sheets are read through `--export` and `--import`.
API_KINDS = (source.PRICING_PDF,)

# US dollars per million tokens, input then output.
PRICES_PER_MILLION_TOKENS = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.1, 0.5),
}
MAX_OUTPUT_TOKENS = 64000
# Models that take the server-side refusal fallback in its `default` form. A
# Haiku has none, so a refusal there leaves the document failed.
FALLBACK_MODELS = frozenset(
    {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
)

_DOLLAR_AMOUNT_PATTERN = re.compile(r"\$\s?\d")

_FUEL_ECONOMY_COLUMNS = {
    claim.MPG_CITY: "city08",
    claim.MPG_HIGHWAY: "highway08",
    claim.MPG_COMBINED: "comb08",
}
# What the file says a row is, kept as printed for the resolve stage to join on.
_FUEL_ECONOMY_SCOPE_COLUMNS = (
    "model",
    "baseModel",
    "trany",
    "drive",
    "displ",
    "cylinders",
    "fuelType1",
    "atvType",
    "eng_dscr",
    "VClass",
)


class _Answer(BaseModel):
    # The API's structured output needs every object closed to extra fields.
    model_config = ConfigDict(extra="forbid")


class DatedQuote(_Answer):
    date: str
    quote: str
    page: int


class PriceBasis(_Answer):
    delivery_fee: Literal["included", "excluded", "unstated"]
    quote: str | None
    page: int | None


class QuotedStatement(_Answer):
    quote: str
    page: int


class DeliveryFee(_Answer):
    amount: str
    vehicle_class: str
    models: list[str]
    effective_date: str | None
    quote: str
    page: int


class PriceColumns(_Answer):
    model_code: str | None
    model_year: str | None
    description: str | None
    msrp: str | None


class PricingRow(_Answer):
    page: int
    table: int
    row_label: str
    model_code: str | None
    model_year: str
    description: str
    msrp: str
    model: str
    powertrain: str | None
    grade: str | None
    drive: str | None
    engine: str | None
    transmission: str | None
    body: str | None


class PricingAnswer(_Answer):
    document_date: DatedQuote | None
    price_basis: PriceBasis
    price_effective: QuotedStatement | None
    delivery_fees: list[DeliveryFee]
    columns: PriceColumns
    rows: list[PricingRow]


class ValueScope(_Answer):
    grades: list[str]
    drive: str | None
    engine: str | None
    transmission: str | None
    body: str | None


class ValueLocator(_Answer):
    kind: Literal["table_cell", "text"]
    page: int
    table: int | None
    row_label: str | None
    column_header: str | None


class SpecificationValue(_Answer):
    attribute: Literal["horsepower", "torque", "mpg_city", "mpg_highway", "mpg_combined"]
    value: str
    unit: str
    describes: str | None
    quote: str
    applies_to: ValueScope
    hedges: list[str]
    locator: ValueLocator


class SpecificationAnswer(_Answer):
    model_year: int
    model: str
    document_date: DatedQuote | None
    values: list[SpecificationValue]


ANSWERS_BY_KIND: dict[str, type[PricingAnswer] | type[SpecificationAnswer]] = {
    source.PRICING_PDF: PricingAnswer,
    source.PRODUCT_INFORMATION_PDF: SpecificationAnswer,
}


@dataclass(frozen=True, slots=True)
class ExtractionRequest:
    """Everything a model needs to read one document."""

    snapshot_sha256: str
    url: str
    kind: str
    prompt_name: str
    system: str
    document: str
    schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExtractorResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class Extractor(Protocol):
    """Something that answers an extraction request. `label` names it in the data."""

    label: str

    def extract(self, request: ExtractionRequest) -> ExtractorResponse: ...


class ExtractionError(Exception):
    """The extractor returned no usable answer."""


@dataclass(slots=True)
class ExtractionReport:
    extracted: list[str] = field(default_factory=list)
    already_held: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    model_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def describe(self) -> str:
        return (
            f"{len(self.extracted)} extracted, {len(self.already_held)} already held, "
            f"{len(self.failed)} failed; {self.model_call_count} model calls, "
            f"{self.input_tokens} input and {self.output_tokens} output tokens, "
            f"${self.cost_usd:.2f}"
        )


class AnthropicExtractor:
    """Reads a document with one Claude API call and a structured output schema."""

    def __init__(
        self,
        model: str,
        effort: Literal["low", "medium", "high", "xhigh", "max"],
        *,
        is_thinking: bool = True,
        api_key: str | None = None,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        # An answer read without thinking is kept apart from one read with it.
        self.label = model if is_thinking else f"{model}/no-thinking"
        self._model = model
        self._effort = effort
        self._is_thinking = is_thinking
        # With no key given, the SDK resolves ANTHROPIC_API_KEY from the
        # environment or an `ant auth login` profile.
        self._client = client or anthropic.Anthropic(api_key=api_key)

    def extract(self, request: ExtractionRequest) -> ExtractorResponse:
        # Streamed, because a pricing document's answer runs to hundreds of
        # rows. Where the model allows it, a request a safety classifier
        # declines is re-run server-side on the model Anthropic recommends for
        # that refusal category.
        has_fallback = self._model in FALLBACK_MODELS
        with self._client.beta.messages.stream(
            model=self._model,
            max_tokens=MAX_OUTPUT_TOKENS,
            betas=["server-side-fallback-2026-07-01"] if has_fallback else anthropic.omit,
            fallbacks="default" if has_fallback else anthropic.omit,
            thinking={"type": "adaptive"} if self._is_thinking else {"type": "disabled"},
            system=request.system,
            messages=[{"role": "user", "content": request.document}],
            output_config={
                "effort": self._effort,
                "format": {"type": "json_schema", "schema": request.schema},
            },
        ) as stream:
            message = stream.get_final_message()
        if message.stop_reason == "refusal":
            raise ExtractionError("the model declined the request")
        if message.stop_reason == "max_tokens":
            raise ExtractionError(f"the answer was cut off at {MAX_OUTPUT_TOKENS} tokens")
        text = next((block.text for block in message.content if block.type == "text"), None)
        if text is None:
            raise ExtractionError("the answer holds no text")
        input_price, output_price = PRICES_PER_MILLION_TOKENS.get(message.model, (0.0, 0.0))
        usage = message.usage
        cost = (usage.input_tokens * input_price + usage.output_tokens * output_price) / 1e6
        return ExtractorResponse(text, usage.input_tokens, usage.output_tokens, cost)


def get_extractor_version(label: str, kind: str) -> str:
    """The extractor's name and the prompt's name: what an answer is keyed on."""
    return f"{label}+{PROMPTS_BY_KIND[kind]}"


def render_document(text: str, tables: tuple[document.Table, ...]) -> str:
    """Lay a snapshot out for the model: each page's text, then its tables."""
    parts: list[str] = []
    for page, page_text in enumerate(text.split(document.PAGE_BREAK), start=1):
        parts.append(f"===== PAGE {page} =====\n{page_text.strip()}")
        for table in tables:
            if table.page != page:
                continue
            rows = "\n".join(" | ".join(row) for row in table.rows)
            parts.append(f"--- TABLE (page {table.page}, table {table.index}) ---\n{rows}")
    return "\n\n".join(parts)


def build_request(snapshot: Snapshot) -> ExtractionRequest:
    prompt_name = PROMPTS_BY_KIND[snapshot.kind]
    return ExtractionRequest(
        snapshot_sha256=snapshot.sha256,
        url=snapshot.url,
        kind=snapshot.kind,
        prompt_name=prompt_name,
        system=(PROMPT_DIR / f"{prompt_name}.md").read_text(encoding="utf-8"),
        document=render_document(snapshot.read_text(), snapshot.read_tables()),
        schema=ANSWERS_BY_KIND[snapshot.kind].model_json_schema(),
    )


def find_extraction_status(
    connection: duckdb.DuckDBPyConnection, snapshot_sha256: str, extractor_version: str
) -> str | None:
    """`done` or `failed` for an answer the pipeline holds, or None when it holds none."""
    found = connection.execute(
        "SELECT status FROM extractions WHERE snapshot_sha256 = ? AND extractor_version = ?",
        [snapshot_sha256, extractor_version],
    ).fetchone()
    return found[0] if found else None


def extract_documents(
    connection: duckdb.DuckDBPyConnection,
    settings: Settings,
    extractor: Extractor,
    *,
    kinds: tuple[str, ...] = tuple(PROMPTS_BY_KIND),
    is_retrying_failed: bool = False,
) -> ExtractionReport:
    """Read every document snapshot of the given kinds this extractor has not read yet.

    A document whose extraction failed is not read again unless told to, so a
    second run makes no model calls.
    """
    report = ExtractionReport()
    for snapshot in list_snapshots(connection, settings, kinds=kinds):
        extractor_version = get_extractor_version(extractor.label, snapshot.kind)
        status = find_extraction_status(connection, snapshot.sha256, extractor_version)
        if status == "done":
            report.already_held.append(snapshot.url)
            continue
        if status == "failed" and not is_retrying_failed:
            report.failed.append(snapshot.url)
            continue
        report.model_call_count += 1
        try:
            response = extractor.extract(build_request(snapshot))
        except anthropic.AuthenticationError, anthropic.PermissionDeniedError:
            # Wrong credentials fail every document alike: stop, record nothing.
            raise
        except (ExtractionError, anthropic.APIError) as error:
            _store_extraction(
                connection, snapshot.sha256, extractor_version, "failed", "", str(error)
            )
            report.failed.append(snapshot.url)
            continue
        report.input_tokens += response.input_tokens
        report.output_tokens += response.output_tokens
        report.cost_usd += response.cost_usd
        problem = record_answer(connection, snapshot, extractor_version, response)
        (report.failed if problem else report.extracted).append(snapshot.url)
    return report


def export_requests(
    connection: duckdb.DuckDBPyConnection,
    settings: Settings,
    label: str,
    directory: Path,
    *,
    model_years: tuple[int, ...] = (),
) -> list[Path]:
    """Write a request file for every document the labelled extractor has not read.

    The request file holds everything, for a program. Beside it go the
    document as plain text and the answer's schema, for a reader. Model years
    narrow the export to documents discovered for those years.
    """
    directory.mkdir(parents=True, exist_ok=True)
    wanted = {
        candidate.url for candidate in source.list_candidates(connection, model_years=model_years)
    }
    written: list[Path] = []
    for snapshot in list_snapshots(connection, settings, kinds=tuple(PROMPTS_BY_KIND)):
        extractor_version = get_extractor_version(label, snapshot.kind)
        if find_extraction_status(connection, snapshot.sha256, extractor_version) == "done":
            continue
        if model_years and snapshot.url not in wanted:
            continue
        request = build_request(snapshot)
        stem = directory / snapshot.sha256
        document_path = stem.with_suffix(".document.txt")
        schema_path = stem.with_suffix(".schema.json")
        document_path.write_text(request.document, encoding="utf-8")
        schema_path.write_text(json.dumps(request.schema, indent=1), encoding="utf-8")
        path = stem.with_suffix(".request.json")
        path.write_text(
            json.dumps(
                {
                    "snapshot_sha256": request.snapshot_sha256,
                    "url": request.url,
                    "kind": request.kind,
                    "extractor_version": extractor_version,
                    "instructions_file": str(PROMPT_DIR / f"{request.prompt_name}.md"),
                    "document_file": document_path.name,
                    "schema_file": schema_path.name,
                    "answer_file": stem.with_suffix(".answer.json").name,
                    "system": request.system,
                    "document": request.document,
                    "schema": request.schema,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        written.append(path)
    return written


def import_answers(
    connection: duckdb.DuckDBPyConnection, settings: Settings, label: str, directory: Path
) -> ExtractionReport:
    """Read the answer files a labelled extractor left beside its request files.

    An answer that failed the schema before is read again, since reading a
    corrected file costs no model call.
    """
    report = ExtractionReport()
    for snapshot in list_snapshots(connection, settings, kinds=tuple(PROMPTS_BY_KIND)):
        extractor_version = get_extractor_version(label, snapshot.kind)
        if find_extraction_status(connection, snapshot.sha256, extractor_version) == "done":
            report.already_held.append(snapshot.url)
            continue
        path = directory / f"{snapshot.sha256}.answer.json"
        if not path.is_file():
            continue
        response = ExtractorResponse(path.read_text(encoding="utf-8"))
        problem = record_answer(connection, snapshot, extractor_version, response)
        (report.failed if problem else report.extracted).append(snapshot.url)
    return report


def check_answer_file(request_path: Path, answer_path: Path) -> tuple[bool, str]:
    """Check an answer file against its request's schema, without the database.

    Returns whether the answer is usable, and a summary for whoever wrote it.
    For a pricing document the summary also counts the table rows that print
    a dollar amount, so a short answer shows before it costs a style.
    """
    request = json.loads(request_path.read_text(encoding="utf-8"))
    try:
        answer = ANSWERS_BY_KIND[request["kind"]].model_validate_json(
            answer_path.read_text(encoding="utf-8")
        )
    except ValidationError as error:
        lines = [f"{answer_path.name}: does not fit the schema, {error.error_count()} errors"]
        for problem in error.errors()[:15]:
            where = ".".join(str(part) for part in problem["loc"])
            lines.append(f"  {where}: {problem['msg']}")
        return False, "\n".join(lines)
    if isinstance(answer, SpecificationAnswer):
        return True, f"{answer_path.name}: fits the schema, {len(answer.values)} values"
    priced_rows = sum(
        1
        for line in request["document"].splitlines()
        if " | " in line and _DOLLAR_AMOUNT_PATTERN.search(line)
    )
    return True, (
        f"{answer_path.name}: fits the schema, {len(answer.rows)} rows and "
        f"{len(answer.delivery_fees)} delivery fees. The document's tables hold "
        f"{priced_rows} rows that print a dollar amount."
    )


def record_answer(
    connection: duckdb.DuckDBPyConnection,
    snapshot: Snapshot,
    extractor_version: str,
    response: ExtractorResponse,
) -> str | None:
    """Keep an extractor's answer and turn it into claims. Returns a problem, if any.

    An answer that does not fit the schema is kept as a failure and yields no
    claims. A newer answer for a snapshot replaces the claims of an older one.
    """
    try:
        answer = ANSWERS_BY_KIND[snapshot.kind].model_validate_json(response.text)
    except ValidationError as error:
        problem = f"the answer does not fit the schema: {error.error_count()} errors"
        _store_extraction(
            connection, snapshot.sha256, extractor_version, "failed", response.text, problem
        )
        return problem
    document_date = check_document_date(answer.document_date, snapshot.read_text())
    if isinstance(answer, PricingAnswer):
        styles, values = build_pricing_claims(answer, snapshot, extractor_version, document_date)
    else:
        styles = []
        values = build_specification_claims(answer, snapshot, extractor_version, document_date)
    claim.delete_claims(connection, snapshot.sha256)
    claim.store_style_claims(connection, styles)
    claim.store_value_claims(connection, values)
    connection.execute(
        "UPDATE snapshots SET document_date = ? WHERE sha256 = ?", [document_date, snapshot.sha256]
    )
    detail = f"{len(styles)} style claims, {len(values)} value claims"
    _store_extraction(
        connection, snapshot.sha256, extractor_version, "done", response.text, detail, response
    )
    return None


def check_document_date(stated: DatedQuote | None, text: str) -> date | None:
    """Admit a document's date only if its quote is in the text and states that date."""
    if stated is None:
        return None
    try:
        parsed = date.fromisoformat(stated.date)
    except ValueError:
        return None
    if evidence.find_quote(stated.quote, text, page=stated.page) is None:
        return None
    numbers = evidence.list_numbers(stated.quote)
    month_names = (parsed.strftime("%B").lower(), parsed.strftime("%b").lower())
    has_month = parsed.month in numbers or any(
        name in evidence.normalize_for_matching(stated.quote) for name in month_names
    )
    # A two-digit year counts only where the quote prints no four-digit one,
    # and only if it is not just the day or the month seen again.
    short_year = Decimal(parsed.year % 100)
    needed = 1 + (parsed.day == short_year) + (parsed.month == short_year)
    has_short_year = not any(number >= 1000 for number in numbers) and (
        numbers.count(short_year) >= needed
    )
    has_year = parsed.year in numbers or has_short_year
    return parsed if has_year and has_month and parsed.day in numbers else None


def build_pricing_claims(
    answer: PricingAnswer,
    snapshot: Snapshot,
    extractor_version: str,
    document_date: date | None,
) -> tuple[list[StyleClaim], list[ValueClaim]]:
    """One style claim and one MSRP claim per priced row, and a claim per delivery fee."""
    styles: list[StyleClaim] = []
    values: list[ValueClaim] = []
    code_counts: dict[tuple[str, str], int] = {}
    for row in answer.rows:
        if row.model_code:
            key = (row.model_year.strip(), row.model_code.strip())
            code_counts[key] = code_counts.get(key, 0) + 1

    qualifiers = {"delivery_fee": answer.price_basis.delivery_fee}
    if answer.price_effective is not None:
        qualifiers["price_effective"] = answer.price_effective.quote
    for ordinal, row in enumerate(answer.rows):
        model_year = evidence.parse_number(row.model_year)
        if model_year is None:
            continue
        code = row.model_code.strip() if row.model_code else None
        style_key = claim.build_style_key(
            int(model_year),
            model_code=code,
            is_code_unique=code is not None and code_counts[(row.model_year.strip(), code)] == 1,
            model=row.model,
            grade=row.grade,
            powertrain=row.powertrain,
            body=row.body,
            engine=row.engine,
            transmission=row.transmission,
            drive=row.drive,
        )
        styles.append(
            StyleClaim(
                claim_id=claim.build_claim_id(snapshot.sha256, extractor_version, "style", ordinal),
                style_key=style_key,
                model_year=int(model_year),
                model=row.model,
                model_code=code,
                grade=row.grade,
                powertrain=row.powertrain,
                body=row.body,
                engine=row.engine,
                transmission=row.transmission,
                drive=row.drive,
                description=row.description,
                snapshot_sha256=snapshot.sha256,
                locator=TableCell(
                    row.page, row.table, row.row_label, answer.columns.description or ""
                ),
                quote=row.description,
                extractor_version=extractor_version,
            )
        )
        price = evidence.parse_number(row.msrp)
        values.append(
            ValueClaim(
                claim_id=claim.build_claim_id(snapshot.sha256, extractor_version, "msrp", ordinal),
                style_key=style_key,
                applies_to={},
                attribute=claim.MSRP,
                value_text=row.msrp,
                value_num=float(price) if price is not None else None,
                unit="USD",
                snapshot_sha256=snapshot.sha256,
                locator=TableCell(row.page, row.table, row.row_label, answer.columns.msrp or ""),
                quote=row.msrp,
                extractor_version=extractor_version,
                source_class=claim.OEM_PRICING,
                qualifiers=dict(qualifiers),
                effective_date=document_date,
            )
        )

    for ordinal, fee in enumerate(answer.delivery_fees):
        amount = evidence.parse_number(fee.amount)
        values.append(
            ValueClaim(
                claim_id=claim.build_claim_id(snapshot.sha256, extractor_version, "fee", ordinal),
                style_key=None,
                applies_to={"vehicle_class": fee.vehicle_class, "models": fee.models},
                attribute=claim.DELIVERY_FEE,
                value_text=fee.amount,
                value_num=float(amount) if amount is not None else None,
                unit="USD",
                snapshot_sha256=snapshot.sha256,
                locator=PageHint(fee.page),
                quote=fee.quote,
                extractor_version=extractor_version,
                source_class=claim.OEM_PRICING,
                effective_date=_parse_date(fee.effective_date) or document_date,
            )
        )
    return styles, values


def build_specification_claims(
    answer: SpecificationAnswer,
    snapshot: Snapshot,
    extractor_version: str,
    document_date: date | None,
) -> list[ValueClaim]:
    """One value claim per figure, scoped to what the document says it applies to."""
    values: list[ValueClaim] = []
    for ordinal, stated in enumerate(answer.values):
        scope: dict[str, Any] = {
            "model_year": answer.model_year,
            "model": answer.model,
            **stated.applies_to.model_dump(),
        }
        where = stated.locator
        locator: TableCell | PageHint
        if where.kind == "table_cell" and where.table is not None:
            locator = TableCell(
                where.page, where.table, where.row_label or "", where.column_header or ""
            )
        else:
            locator = PageHint(where.page)
        number = evidence.parse_number(stated.value)
        values.append(
            ValueClaim(
                claim_id=claim.build_claim_id(snapshot.sha256, extractor_version, "value", ordinal),
                style_key=None,
                applies_to=scope,
                attribute=stated.attribute,
                value_text=stated.value,
                value_num=float(number) if number is not None else None,
                unit=stated.unit,
                snapshot_sha256=snapshot.sha256,
                locator=locator,
                quote=stated.quote,
                extractor_version=extractor_version,
                source_class=claim.OEM_SPECIFICATION,
                qualifiers={"describes": stated.describes} if stated.describes else {},
                hedge_flags=tuple(stated.hedges),
                effective_date=document_date,
            )
        )
    return values


def extract_fuel_economy(
    connection: duckdb.DuckDBPyConnection, settings: Settings, registry: Registry
) -> ExtractionReport:
    """Parse the fuel economy file into mpg claims. No model is involved.

    Each row of the registry's make and model years gives three claims. A row
    has no grade field, so a claim keeps the row's own description of itself
    and the resolve stage decides which styles it covers.
    """
    report = ExtractionReport()
    for snapshot in list_snapshots(connection, settings, kinds=(source.FUEL_ECONOMY_FILE,)):
        if find_extraction_status(connection, snapshot.sha256, FUEL_ECONOMY_PARSER) == "done":
            report.already_held.append(snapshot.url)
            continue
        values: list[ValueClaim] = []
        for row in csv.DictReader(io.StringIO(snapshot.read_text())):
            year = evidence.parse_number(row.get("year", ""))
            if row.get("make") != registry.make or year not in registry.model_years:
                continue
            scope: dict[str, Any] = {"model_year": int(year)} if year is not None else {}
            scope.update({column: row.get(column, "") for column in _FUEL_ECONOMY_SCOPE_COLUMNS})
            # The file prints no unit. Its figures are miles per gallon of the
            # row's first fuel, and gasoline-equivalent for electricity and hydrogen.
            unit = "MPGe" if row.get("fuelType1") in ("Electricity", "Hydrogen") else "MPG"
            for attribute, column in _FUEL_ECONOMY_COLUMNS.items():
                printed = row.get(column, "")
                number = evidence.parse_number(printed)
                values.append(
                    ValueClaim(
                        claim_id=claim.build_claim_id(
                            snapshot.sha256, FUEL_ECONOMY_PARSER, f"{row['id']}:{column}", 0
                        ),
                        style_key=None,
                        applies_to=scope,
                        attribute=attribute,
                        value_text=printed,
                        value_num=float(number) if number is not None else None,
                        unit=unit,
                        snapshot_sha256=snapshot.sha256,
                        locator=FileRow(row["id"], column),
                        quote=printed,
                        extractor_version=FUEL_ECONOMY_PARSER,
                        source_class=claim.EPA,
                    )
                )
        claim.delete_claims(connection, snapshot.sha256)
        claim.store_value_claims(connection, values)
        detail = f"{len(values)} value claims"
        _store_extraction(connection, snapshot.sha256, FUEL_ECONOMY_PARSER, "done", "", detail)
        report.extracted.append(snapshot.url)
    return report


def _parse_date(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def _store_extraction(
    connection: duckdb.DuckDBPyConnection,
    snapshot_sha256: str,
    extractor_version: str,
    status: str,
    response_text: str,
    detail: str,
    response: ExtractorResponse | None = None,
) -> None:
    usage = response or ExtractorResponse("")
    connection.execute(
        "INSERT OR REPLACE INTO extractions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            snapshot_sha256,
            extractor_version,
            status,
            response_text,
            detail,
            usage.input_tokens,
            usage.output_tokens,
            usage.cost_usd,
            datetime.now(UTC).replace(tzinfo=None),
        ],
    )
