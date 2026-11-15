"""Admissibility rule for extracted claims: grounding.

Language models are fluent at producing plausible car prices, which is exactly
the failure mode that matters here — a hallucinated `$28,495` is
indistinguishable from a real one by inspection. The defence is procedural
rather than statistical: **never accept a model-stated number, only a
model-located one.**

The extractor returns a `verbatim_quote` alongside every value. This module
verifies, in plain code and without a model in the loop, that

1. the quote actually occurs in the text that was fetched, and
2. the value string actually occurs inside the quote.

A record failing either check is rejected, not down-weighted. That single check
removes most of the hallucination surface, because a model cannot fabricate a
number *and* a matching span of source text it never saw.

Ported from the research repository as it stood. Its known limits are S1 work,
set out in docs/plan/design.md under "The evidence check": values are compared
as substrings, so `495` passes inside `$28,495`; the check says pass or fail
but not where the quote was found; and there is no table-cell locator.

Nothing here does I/O.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_WHITESPACE_PATTERN = re.compile(r"\s+")

# Characters that differ between the rendered page and a model's transcription
# of it without changing the text: typographic quotes, dashes, soft hyphens.
_LOOKALIKES = str.maketrans(
    {
        " ": " ",
        " ": " ",
        " ": " ",
        "‘": "'",
        "’": "'",
        "‚": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        "­": "",
    }
)


@dataclass(frozen=True, slots=True)
class Grounding:
    """Whether one extracted claim is anchored in the text that was fetched."""

    is_grounded: bool
    reason: str

    def __bool__(self) -> bool:
        return self.is_grounded


def normalize_for_matching(text: str) -> str:
    """Fold a string to the form both sides of a quote comparison are compared in.

    Unicode-normalises, maps typographic lookalikes to ASCII, collapses runs of
    whitespace, and lowercases. This is deliberately lossy: the check is "did the
    model read this sentence", not "did it transcribe every glyph".
    """
    folded = unicodedata.normalize("NFKC", text).translate(_LOOKALIKES)
    return _WHITESPACE_PATTERN.sub(" ", folded).strip().lower()


def is_quote_present(quote: str, source_text: str) -> bool:
    """Whether the verbatim quote occurs in the fetched source text."""
    if not quote.strip():
        return False
    return normalize_for_matching(quote) in normalize_for_matching(source_text)


def check_grounding(quote: str, value: str, source_text: str) -> Grounding:
    """Verify an extracted value against its quote and the quote against the source.

    Both directions are required. A quote that appears in the source but omits
    the value proves only that the model read *something*; a value inside a quote
    that never appears in the source is a fabrication with a citation attached.
    """
    if not quote.strip():
        return Grounding(False, "empty quote")
    if not value.strip():
        return Grounding(False, "empty value")
    if normalize_for_matching(value) not in normalize_for_matching(quote):
        return Grounding(False, "value absent from its own quote")
    if not is_quote_present(quote, source_text):
        return Grounding(False, "quote absent from fetched source text")
    return Grounding(True, "quote and value both present in source")
