"""Consistency checks: what the pipeline's own data says about itself.

Each check is a query over the claims and the resolved styles. None reads the
reference. A finding says where to look and changes nothing: no claim is
rejected and no value is replaced because a check fired, so a printing error
in a document stays visible beside the value the document printed.

The checks:

- `drive_price_order`: a four-wheel-drive style priced below the two-wheel-
  drive style that is otherwise the same.
- `year_over_year_price`: a model code whose price moved outside a band
  between two model years.
- `fuel_economy_agreement`: a fuel economy figure Toyota's document states
  that differs from the EPA file's for the same style.
- `repeated_model_code`: one model code on rows a document describes
  differently.
- `repeated_description`: one description priced twice in a document, at
  different prices.

No document read so far prints a total beside a price and a fee, so there is
no arithmetic check yet. It belongs here when one does.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import duckdb

from car_surveyor import storage

# How far a model code's price may move from one model year to the next
# before it is worth a look.
MAX_YEARLY_DECREASE = 0.05
MAX_YEARLY_INCREASE = 0.15

_SAME_EXCEPT_DRIVE = " AND ".join(
    f"coalesce(lower(four.{column}), '') = coalesce(lower(two.{column}), '')"
    for column in ("model", "grade", "powertrain", "body", "engine", "transmission_class")
)

CHECKS: dict[str, str] = {
    "drive_price_order": f"""
        SELECT four.style_key, 'msrp' AS attribute,
            format('{{}} at ${{:d}} is priced below {{}} at ${{:d}}',
                four.description, four.msrp::BIGINT, two.description, two.msrp::BIGINT) AS detail
        FROM styles AS four
        JOIN styles AS two ON four.model_year = two.model_year AND {_SAME_EXCEPT_DRIVE}
        WHERE four.drive_class = '4wd' AND two.drive_class = '2wd' AND four.msrp < two.msrp
    """,
    "year_over_year_price": f"""
        SELECT later.style_key, 'msrp' AS attribute,
            format('{{}}: ${{:d}} in {{}} after ${{:d}} in {{}}', later.description,
                later.msrp::BIGINT, later.model_year, earlier.msrp::BIGINT,
                earlier.model_year) AS detail
        FROM styles AS later
        JOIN styles AS earlier
            ON earlier.model_code = later.model_code
            AND lower(earlier.model) = lower(later.model)
            AND earlier.model_year = later.model_year - 1
        WHERE later.msrp < earlier.msrp * (1 - {MAX_YEARLY_DECREASE})
            OR later.msrp > earlier.msrp * (1 + {MAX_YEARLY_INCREASE})
    """,
    "fuel_economy_agreement": """
        WITH ranked AS (
            SELECT style_key, attribute, source_class, value_num,
                rank() OVER (
                    PARTITION BY style_key, attribute, source_class ORDER BY specificity DESC
                ) AS place
            FROM style_values
            WHERE attribute IN ('mpg_city', 'mpg_highway', 'mpg_combined')
        ),
        stated AS (
            SELECT style_key, attribute, source_class,
                min(value_num) AS low, max(value_num) AS high
            FROM ranked WHERE place = 1
            GROUP BY style_key, attribute, source_class
        )
        SELECT oem.style_key, oem.attribute,
            format('{}: the document states {:d} and the EPA file {:d}', held.description,
                oem.low::BIGINT, epa.low::BIGINT) AS detail
        FROM stated AS oem
        JOIN stated AS epa USING (style_key, attribute)
        JOIN style_records AS held USING (style_key)
        WHERE oem.source_class = 'oem_specification' AND epa.source_class = 'epa'
            AND oem.low = oem.high AND epa.low = epa.high
            AND oem.low <> epa.low
    """,
    "repeated_model_code": """
        WITH repeated AS (
            SELECT snapshot_sha256, model_year, model_code
            FROM style_claims
            WHERE status = 'admitted' AND model_code IS NOT NULL
            GROUP BY snapshot_sha256, model_year, model_code
            HAVING count(DISTINCT description) > 1
        )
        SELECT DISTINCT claimed.style_key, NULL AS attribute,
            format('model code {} is also printed on another row; this row is {}',
                claimed.model_code, claimed.description) AS detail
        FROM style_claims AS claimed
        JOIN repeated USING (snapshot_sha256, model_year, model_code)
        WHERE claimed.status = 'admitted'
    """,
    "repeated_description": """
        WITH priced AS (
            SELECT claimed.snapshot_sha256, claimed.model_year, claimed.description,
                claimed.style_key, claimed.model_code, price.value_num
            FROM style_claims AS claimed
            JOIN value_claims AS price
                ON price.style_key = claimed.style_key
                AND price.snapshot_sha256 = claimed.snapshot_sha256
                AND price.attribute = 'msrp' AND price.status = 'admitted'
            WHERE claimed.status = 'admitted'
        ),
        repeated AS (
            SELECT snapshot_sha256, model_year, description
            FROM priced
            GROUP BY snapshot_sha256, model_year, description
            HAVING count(DISTINCT style_key) > 1 AND count(DISTINCT value_num) > 1
        )
        SELECT DISTINCT priced.style_key, 'msrp' AS attribute,
            format('{} is priced twice in one document; this row is {} at ${:d}',
                priced.description, coalesce(priced.model_code, 'uncoded'),
                priced.value_num::BIGINT) AS detail
        FROM priced
        JOIN repeated USING (snapshot_sha256, model_year, description)
    """,
}


@dataclass(frozen=True, slots=True)
class Finding:
    check_name: str
    style_key: str
    attribute: str | None
    detail: str


def run_checks(connection: duckdb.DuckDBPyConnection) -> list[Finding]:
    """Run every check over the resolved tables and keep the findings."""
    findings = [
        Finding(name, record["style_key"], record["attribute"], record["detail"])
        for name, query in CHECKS.items()
        for record in storage.fetch_records(connection, query)
    ]
    findings.sort(key=lambda finding: (finding.check_name, finding.style_key, finding.detail))
    connection.execute("DELETE FROM check_results")
    if findings:
        connection.executemany(
            "INSERT INTO check_results VALUES (?, ?, ?, ?)",
            [
                [finding.check_name, finding.style_key, finding.attribute, finding.detail]
                for finding in findings
            ],
        )
    return findings


def describe_findings(findings: list[Finding]) -> str:
    counts = Counter(finding.check_name for finding in findings)
    lines = [f"{len(findings)} findings"]
    lines += [f"  {counts.get(name, 0):>4}  {name}" for name in CHECKS]
    return "\n".join(lines)
