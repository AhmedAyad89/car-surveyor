"""Tests for grounding. No network.

The fixtures are synthetic text in the style of a Toyota pricing release. The
model name and the figures are made up; nothing here is a source for a value.
"""

from __future__ import annotations

from car_surveyor import evidence

SOURCE = (
    "The 2023 Toyota Example LE has a Manufacturer’s Suggested Retail Price of $28,495. "
    "Pricing excludes the Delivery, Processing and Handling fee of $1,095."
)
QUOTE = "The 2023 Toyota Example LE has a Manufacturer’s Suggested Retail Price of $28,495."


def test_check_grounding_accepts_a_quote_that_is_in_the_source() -> None:
    result = evidence.check_grounding(QUOTE, "$28,495", SOURCE)
    assert result.is_grounded
    assert bool(result)


def test_check_grounding_rejects_a_hallucinated_price() -> None:
    """The failure mode this exists for: a plausible number, no source for it."""
    result = evidence.check_grounding(QUOTE, "$26,495", SOURCE)
    assert not result.is_grounded
    assert result.reason == "value absent from its own quote"


def test_check_grounding_rejects_a_fabricated_quote() -> None:
    fabricated = QUOTE.replace("$28,495", "$26,495")
    result = evidence.check_grounding(fabricated, "$26,495", SOURCE)
    assert not result.is_grounded
    assert result.reason == "quote absent from fetched source text"


def test_check_grounding_rejects_an_empty_quote() -> None:
    assert not evidence.check_grounding("", "$28,495", SOURCE).is_grounded


def test_check_grounding_rejects_an_empty_value() -> None:
    assert not evidence.check_grounding(QUOTE, "  ", SOURCE).is_grounded


def test_check_grounding_tolerates_whitespace_and_typographic_differences() -> None:
    """A model re-typing a quote changes spacing and quote glyphs, not content."""
    retyped = "The 2023 Toyota Example LE   has a Manufacturer's Suggested Retail Price of $28,495."
    assert evidence.check_grounding(retyped, "$28,495", SOURCE).is_grounded


def test_normalize_for_matching_folds_dashes_and_soft_hyphens() -> None:
    folded = evidence.normalize_for_matching("Sug­gested Retail Price – 2023")
    assert folded == "suggested retail price - 2023"


def test_is_quote_present_is_false_for_a_quote_from_another_document() -> None:
    assert not evidence.is_quote_present("The 2023 Toyota Sample XLE starts at $41,250.", SOURCE)
