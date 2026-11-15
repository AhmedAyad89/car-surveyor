"""Tests for the reference loader, on synthetic files shaped as the published schema says.

Nothing here is real reference data: the model, the codes and the figures are
made up. No test reads `~/refdata`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from car_surveyor import claim, reference
from car_surveyor.reference import ReferenceFormatError, ReferenceStyle

EXAMPLE_2023 = """
_meta:
  schema_version: "1.9.0"
  status: populated
  completeness: {powertrain: partial, fuel_economy: full}
model:
  make: Toyota
  model: Example
  year: 2023
  destination_charge: 1095
  body_style: sedan
  powertrain:
    engine: {type: gas, cylinders: 4, displacement_liters: 2.5}
    horsepower: 203
    torque_lb_ft: 184
    drivetrain: {type: fwd}
    transmission: {type: automatic, speeds: 8}
  fuel_economy: {mpg_city: 28, mpg_highway: 39, mpg_combined: 32}
trims:
  le:
    trim: LE
    configuration_axes: [drivetrain]
    fuel_economy: {mpg_highway: 40}
    configurations:
      fwd: {code: "9101", msrp: 28495, drivetrain: FWD}
      awd:
        code: "9102"
        msrp: 29895
        drivetrain: AWD
        discontinued: 2023-06-01
        powertrain: {horsepower: 202, drivetrain: {type: awd}}
        fuel_economy: {mpg_city: 25, mpg_highway: 34, mpg_combined: 29}
  xle:
    trim: XLE
    extends: le
    powertrain: {horsepower: 206}
    configurations:
      fwd: {code: "9105", alternate_codes: ["9106"], msrp: 31250}
  platinum:
    trim: Platinum
    body_style: hatchback
    configurations:
      standard: {msrp: 38400, destination_charge: 1295, seating_capacity: 5, cab: Double Cab}
"""


def _read(text: str = EXAMPLE_2023) -> dict[tuple[str, str], ReferenceStyle]:
    styles = reference.read_reference_file(text, "example/2023.yaml")
    return {(style.trim, style.configuration): style for style in styles}


def test_a_configuration_inherits_from_the_model_and_overrides_it() -> None:
    styles = _read()
    front, all_wheel = styles[("le", "fwd")], styles[("le", "awd")]
    assert (front.msrp, front.codes, front.destination_charge) == (28495, ("9101",), 1095)
    assert (front.horsepower, front.torque, front.cylinders) == (203, 184, 4)
    assert (front.drivetrain_type, front.transmission_type, front.engine_type) == (
        "fwd",
        "automatic",
        "gas",
    )
    assert (all_wheel.horsepower, all_wheel.torque, all_wheel.drivetrain_type) == (202, 184, "awd")
    assert all_wheel.cylinders == 4
    assert (all_wheel.mpg_city, all_wheel.mpg_highway, all_wheel.mpg_combined) == (25, 34, 29)
    assert (front.is_discontinued, all_wheel.is_discontinued) == (False, True)


def test_a_trim_level_value_sits_between_the_model_and_the_configuration() -> None:
    styles = _read()
    assert styles[("le", "fwd")].mpg_highway == 40
    assert styles[("le", "fwd")].mpg_city == 28
    assert styles[("platinum", "standard")].mpg_highway == 39


def test_a_trim_inherits_its_parents_trim_level_values_but_not_its_prices() -> None:
    extended = _read()[("xle", "fwd")]
    assert extended.mpg_highway == 40
    assert extended.horsepower == 206
    assert (extended.msrp, extended.codes) == (31250, ("9105", "9106"))


def test_identity_labels_and_overrides_are_kept() -> None:
    top = _read()[("platinum", "standard")]
    assert (top.file, top.model_slug, top.model, top.trim_name) == (
        "example/2023.yaml",
        "example",
        "Example",
        "Platinum",
    )
    assert (top.model_year, top.is_mid_year) == (2023, False)
    assert top.codes == ()
    assert top.destination_charge == 1295
    assert top.labels == {"seating_capacity": "5", "cab": "Double Cab"}
    assert top.body_style == "hatchback"
    assert _read()[("le", "fwd")].body_style == "sedan"
    assert top.completeness == {"powertrain": "partial", "fuel_economy": "full"}


def test_a_mid_year_file_belongs_to_its_model_year() -> None:
    styles = _read(EXAMPLE_2023.replace("year: 2023", "year: 2023.5"))
    assert {(style.model_year, style.is_mid_year) for style in styles.values()} == {(2023, True)}


def test_get_value_reads_gasoline_equivalent_figures_from_their_own_fields() -> None:
    electric = reference.read_reference_file(
        """
model: {make: Toyota, model: eX4, year: 2023}
trims:
  xle:
    trim: XLE
    configurations:
      fwd: {msrp: 42000, fuel_economy: {mpge_city: 131, mpge_highway: 107, mpge_combined: 119}}
""",
        "ex4/2023.yaml",
    )[0]
    assert electric.get_value(claim.MPG_CITY) is None
    assert electric.get_value(claim.MPG_CITY, is_gasoline_equivalent=True) == 131
    assert electric.get_value(claim.MSRP, is_gasoline_equivalent=True) == 42000
    assert electric.get_value(claim.DELIVERY_FEE) is None


@pytest.mark.parametrize(
    ("broken", "field", "secret"),
    [
        (EXAMPLE_2023.replace("msrp: 31250", 'msrp: "31,250"'), "fwd.msrp", "31,250"),
        (EXAMPLE_2023.replace("horsepower: 206", "horsepower: 206.5"), "horsepower", "206.5"),
        (EXAMPLE_2023.replace("year: 2023", "year: twenty23"), "model.year", "twenty23"),
        (EXAMPLE_2023.replace("extends: le", "extends: limited"), "trims.xle.extends", "limited"),
        (EXAMPLE_2023.replace("model: Example", "model: [Example]"), "model.model", "Example"),
        ("model: {make: Toyota, model: Example, year: 2023}", "trims", "Example"),
        ("trims: {le: {trim: LE, configurations: {fwd: {msrp: 1}}}}", "model", "LE"),
        ("model: [unclosed", "YAML", "unclosed"),
    ],
)
def test_an_error_names_the_file_and_the_field_and_never_the_value(
    broken: str, field: str, secret: str
) -> None:
    with pytest.raises(ReferenceFormatError) as raised:
        reference.read_reference_file(broken, "example/2023.yaml")
    message = str(raised.value)
    assert "example/2023.yaml" in message
    assert field in message
    assert secret not in message


def test_a_trim_that_extends_itself_is_an_error() -> None:
    looped = EXAMPLE_2023.replace("extends: le", "extends: xle")
    with pytest.raises(ReferenceFormatError, match=r"trims\.xle\.extends"):
        reference.read_reference_file(looped, "example/2023.yaml")


def _write_bundle(root: Path) -> None:
    make_dir = root / "data" / "us" / "toyota"
    (make_dir / "example").mkdir(parents=True)
    (make_dir / "example" / "2023.yaml").write_text(EXAMPLE_2023)
    earlier = EXAMPLE_2023.replace("year: 2023", "year: 2022")
    (make_dir / "example" / "2022.yaml").write_text(earlier)
    (make_dir / "lineup.yaml").write_text("market: us\nmake: toyota\nmodels: {}\n")
    other = root / "data" / "us" / "honda" / "sample"
    other.mkdir(parents=True)
    (other / "2023.yaml").write_text(EXAMPLE_2023.replace("Toyota", "Honda"))


def test_load_reference_reads_one_makes_model_year_files(tmp_path: Path) -> None:
    _write_bundle(tmp_path)
    styles = reference.load_reference(tmp_path, "Toyota")
    assert len(styles) == 8
    assert {style.file for style in styles} == {"example/2022.yaml", "example/2023.yaml"}
    assert [style.model_year for style in styles[:4]] == [2022, 2022, 2022, 2022]


def test_load_reference_says_when_the_make_has_no_folder(tmp_path: Path) -> None:
    with pytest.raises(ReferenceFormatError, match="no folder for this make"):
        reference.load_reference(tmp_path, "Toyota")


def test_coverage_counts_what_the_reference_holds_and_prints_no_value(tmp_path: Path) -> None:
    _write_bundle(tmp_path)
    styles = reference.load_reference(tmp_path, "Toyota")
    counts = reference.count_coverage(styles)[2023]
    assert (counts["files"], counts["trims"], counts["configurations"]) == (1, 3, 4)
    assert (counts["with a model code"], counts[claim.MSRP], counts[claim.HORSEPOWER]) == (3, 4, 4)
    printed = reference.describe_coverage(styles)
    assert printed.splitlines()[0].startswith("model year  files  trims  configurations")
    assert len(printed.splitlines()) == 3
    for value in ("28495", "31250", "9101", "Example", "LE"):
        assert value not in printed
