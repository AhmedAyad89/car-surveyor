"""The reference: OpenSpec's files for one make, as one record per configuration.

Only `diff.py` and the reference-coverage command use this module, and only
the user runs those. Nothing here is imported by a pipeline stage.

The layout is OpenSpec's published schema (`docs/reference_schema/`): one YAML
file per model and model year under `data/<market>/<make>/<model>/`, holding
trims, each holding configurations. An attribute set on the model is inherited
by a trim's parent chain, then the trim, then the configuration, and the
innermost value wins.

An error names the file and the field. It never quotes a value, because an
error message is read by whoever is debugging, and that can be an agent.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from car_surveyor import claim

# The display labels a configuration may carry for the axes it varies on.
AXIS_LABELS = (
    "drivetrain",
    "transmission",
    "engine",
    "cab",
    "bed",
    "body",
    "seating_capacity",
    "tier",
    "battery",
    "wheelbase",
    "rear_wheels",
)
# Where each pipeline attribute lives in a reference record.
FIELDS_BY_ATTRIBUTE = {
    claim.MSRP: "msrp",
    claim.DELIVERY_FEE: "destination_charge",
    claim.HORSEPOWER: "horsepower",
    claim.TORQUE: "torque",
    claim.MPG_CITY: "mpg_city",
    claim.MPG_HIGHWAY: "mpg_highway",
    claim.MPG_COMBINED: "mpg_combined",
}
MPGE_FIELDS_BY_ATTRIBUTE = {
    claim.MPG_CITY: "mpge_city",
    claim.MPG_HIGHWAY: "mpge_highway",
    claim.MPG_COMBINED: "mpge_combined",
}


class ReferenceFormatError(Exception):
    """A reference file is not shaped as the schema says. Names the file and field only."""


@dataclass(frozen=True, slots=True)
class ReferenceStyle:
    """One configuration of one trim, with what it inherits resolved."""

    file: str
    model_year: int
    is_mid_year: bool
    model_slug: str
    model: str
    trim: str
    trim_name: str
    configuration: str
    codes: tuple[str, ...]
    labels: dict[str, str]
    msrp: int | None = None
    destination_charge: int | None = None
    horsepower: int | None = None
    torque: int | None = None
    mpg_city: int | None = None
    mpg_highway: int | None = None
    mpg_combined: int | None = None
    mpge_city: int | None = None
    mpge_highway: int | None = None
    mpge_combined: int | None = None
    engine_type: str | None = None
    cylinders: int | None = None
    drivetrain_type: str | None = None
    transmission_type: str | None = None
    body_style: str | None = None
    is_discontinued: bool = False
    completeness: dict[str, str] = field(default_factory=dict)

    def get_value(self, attribute: str, *, is_gasoline_equivalent: bool = False) -> int | None:
        """The reference's value for a pipeline attribute, or None where it has none.

        Fuel economy in gasoline-equivalent miles is a separate field in the
        reference, so the caller says which the pipeline's figure is.
        """
        fields = MPGE_FIELDS_BY_ATTRIBUTE if is_gasoline_equivalent else FIELDS_BY_ATTRIBUTE
        name = fields.get(attribute, FIELDS_BY_ATTRIBUTE[attribute])
        value: int | None = getattr(self, name)
        return value


def load_reference(reference_dir: Path, make: str, market: str = "us") -> list[ReferenceStyle]:
    """Every configuration the reference holds for a make, in file order."""
    make_dir = reference_dir / "data" / market / claim.slugify(make).replace("-", "_")
    if not make_dir.is_dir():
        raise ReferenceFormatError(f"{make_dir.name}: the reference has no folder for this make")
    styles: list[ReferenceStyle] = []
    for path in sorted(make_dir.glob("*/*.yaml")):
        relative = f"{path.parent.name}/{path.name}"
        styles.extend(read_reference_file(path.read_text(encoding="utf-8"), relative))
    return styles


def read_reference_file(text: str, relative: str) -> list[ReferenceStyle]:
    """Flatten one model-year file into its configurations."""
    try:
        content = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ReferenceFormatError(f"{relative}: not readable as YAML") from error
    model = _get_mapping(content, "model", relative, "the file")
    trims = _get_mapping(content, "trims", relative, "the file")
    meta = content.get("_meta") if isinstance(content, Mapping) else None
    completeness = meta.get("completeness") if isinstance(meta, Mapping) else None

    year = model.get("year")
    if isinstance(year, bool) or not isinstance(year, int | float):
        raise ReferenceFormatError(f"{relative}: model.year: expected a number")
    name = model.get("model")
    if not isinstance(name, str):
        raise ReferenceFormatError(f"{relative}: model.model: expected text")

    styles: list[ReferenceStyle] = []
    for trim_slug, trim in trims.items():
        where = f"trims.{trim_slug}"
        if not isinstance(trim, Mapping):
            raise ReferenceFormatError(f"{relative}: {where}: expected a mapping")
        chain = [model, *_list_parents(trims, str(trim_slug), relative), trim]
        configurations = _get_mapping(trim, "configurations", relative, where)
        for slug, configuration in configurations.items():
            place = f"{where}.configurations.{slug}"
            if not isinstance(configuration, Mapping):
                raise ReferenceFormatError(f"{relative}: {place}: expected a mapping")
            levels = [*chain, configuration]
            powertrain = _merge_levels(levels, "powertrain")
            economy = _merge_levels(levels, "fuel_economy")
            codes = [configuration.get("code"), *(configuration.get("alternate_codes") or [])]
            charge = configuration.get("destination_charge", model.get("destination_charge"))
            charge_place = f"{place}.destination_charge"
            engine = powertrain.get("engine") or {}
            styles.append(
                ReferenceStyle(
                    file=relative,
                    model_year=int(year),
                    is_mid_year=year != int(year),
                    model_slug=relative.split("/")[0],
                    model=name,
                    trim=str(trim_slug),
                    trim_name=str(trim.get("trim") or trim_slug),
                    configuration=str(slug),
                    codes=tuple(str(code).strip() for code in codes if code is not None),
                    labels={
                        axis: str(configuration[axis])
                        for axis in AXIS_LABELS
                        if configuration.get(axis) is not None
                    },
                    msrp=_get_whole(configuration, "msrp", relative, place),
                    destination_charge=_check_whole(charge, relative, charge_place),
                    horsepower=_get_whole(powertrain, "horsepower", relative, place),
                    torque=_get_whole(powertrain, "torque_lb_ft", relative, place),
                    mpg_city=_get_whole(economy, "mpg_city", relative, place),
                    mpg_highway=_get_whole(economy, "mpg_highway", relative, place),
                    mpg_combined=_get_whole(economy, "mpg_combined", relative, place),
                    mpge_city=_get_whole(economy, "mpge_city", relative, place),
                    mpge_highway=_get_whole(economy, "mpge_highway", relative, place),
                    mpge_combined=_get_whole(economy, "mpge_combined", relative, place),
                    engine_type=_get_text(engine, "type"),
                    cylinders=_get_whole(engine, "cylinders", relative, place),
                    drivetrain_type=_get_text(powertrain.get("drivetrain"), "type"),
                    transmission_type=_get_text(powertrain.get("transmission"), "type"),
                    body_style=next(
                        (
                            str(level["body_style"])
                            for level in reversed(levels)
                            if level.get("body_style")
                        ),
                        None,
                    ),
                    is_discontinued=configuration.get("discontinued") is not None,
                    completeness=dict(completeness) if isinstance(completeness, Mapping) else {},
                )
            )
    return styles


def count_coverage(styles: list[ReferenceStyle]) -> dict[int, Counter[str]]:
    """Per model year: how many files, trims and configurations, and how many hold each value.

    Counts only. This is what `reference-coverage` prints.
    """
    counts: dict[int, Counter[str]] = {}
    seen: set[tuple[int, str, str]] = set()
    for style in styles:
        year = counts.setdefault(style.model_year, Counter())
        for kind, key in (("files", style.file), ("trims", f"{style.file}:{style.trim}")):
            if (style.model_year, kind, key) not in seen:
                seen.add((style.model_year, kind, key))
                year[kind] += 1
        year["configurations"] += 1
        year["with a model code"] += bool(style.codes)
        for attribute, name in FIELDS_BY_ATTRIBUTE.items():
            year[attribute] += getattr(style, name) is not None
    return counts


def describe_coverage(styles: list[ReferenceStyle]) -> str:
    counts = count_coverage(styles)
    columns = ["files", "trims", "configurations", "with a model code", *FIELDS_BY_ATTRIBUTE]
    lines = ["model year  " + "  ".join(columns)]
    for year in sorted(counts):
        cells = "  ".join(f"{counts[year][column]:>{len(column)}}" for column in columns)
        lines.append(f"{year:<10}  {cells}")
    return "\n".join(lines)


def _list_parents(
    trims: Mapping[str, Any], trim_slug: str, relative: str
) -> list[Mapping[str, Any]]:
    """A trim's `extends` chain, outermost parent first."""
    parents: list[Mapping[str, Any]] = []
    seen = {trim_slug}
    current = trims[trim_slug]
    while isinstance(current, Mapping) and current.get("extends") is not None:
        parent_slug = str(current["extends"])
        parent = trims.get(parent_slug)
        if parent_slug in seen or not isinstance(parent, Mapping):
            raise ReferenceFormatError(
                f"{relative}: trims.{trim_slug}.extends: not a chain of other trims in this file"
            )
        seen.add(parent_slug)
        parents.insert(0, parent)
        current = parent
    return parents


def _merge_levels(levels: list[Mapping[str, Any]], section: str) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for level in levels:
        _merge_into(merged, level.get(section) or {})
    return merged


def _merge_into(target: dict[str, Any], override: Mapping[str, Any]) -> None:
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _merge_into(target[key], value)
        elif isinstance(value, Mapping):
            target[key] = {}
            _merge_into(target[key], value)
        else:
            target[key] = value


def _get_mapping(holder: Any, key: str, relative: str, where: str) -> Mapping[str, Any]:
    value = holder.get(key) if isinstance(holder, Mapping) else None
    if not isinstance(value, Mapping):
        raise ReferenceFormatError(f"{relative}: {where} has no mapping named {key}")
    return value


def _get_whole(holder: Mapping[str, Any], key: str, relative: str, where: str) -> int | None:
    return _check_whole(holder.get(key), relative, f"{where}.{key}")


def _check_whole(value: Any, relative: str, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReferenceFormatError(f"{relative}: {where}: expected a whole number")
    return value


def _get_text(holder: Any, key: str) -> str | None:
    value = holder.get(key) if isinstance(holder, Mapping) else None
    return str(value) if value is not None else None
