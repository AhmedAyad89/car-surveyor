-- The query views over the tables the resolve stage rebuilds.
-- `style_records` has one row per style and `style_values` one row per
-- admitted claim attached to a style. Nothing here reads the reference.

-- One row per style, with the chosen value of each attribute, the claim it
-- came from, and its date. An empty cell means no source stated the value,
-- or the sources that did disagree (see `disagreements`).
CREATE OR REPLACE VIEW styles AS
SELECT
    r.*,
    max(CASE WHEN v.attribute = 'msrp' THEN v.value_num END) AS msrp,
    max(CASE WHEN v.attribute = 'msrp' THEN v.claim_id END) AS msrp_claim,
    max(CASE WHEN v.attribute = 'msrp' THEN v.effective_date END) AS msrp_date,
    max(CASE WHEN v.attribute = 'delivery_fee' THEN v.value_num END) AS delivery_fee,
    max(CASE WHEN v.attribute = 'delivery_fee' THEN v.claim_id END) AS delivery_fee_claim,
    max(CASE WHEN v.attribute = 'delivery_fee' THEN v.effective_date END) AS delivery_fee_date,
    max(CASE WHEN v.attribute = 'horsepower' THEN v.value_num END) AS horsepower,
    max(CASE WHEN v.attribute = 'horsepower' THEN v.claim_id END) AS horsepower_claim,
    max(CASE WHEN v.attribute = 'horsepower' THEN v.effective_date END) AS horsepower_date,
    max(CASE WHEN v.attribute = 'torque' THEN v.value_num END) AS torque,
    max(CASE WHEN v.attribute = 'torque' THEN v.claim_id END) AS torque_claim,
    max(CASE WHEN v.attribute = 'torque' THEN v.effective_date END) AS torque_date,
    max(CASE WHEN v.attribute = 'mpg_city' THEN v.value_num END) AS mpg_city,
    max(CASE WHEN v.attribute = 'mpg_city' THEN v.claim_id END) AS mpg_city_claim,
    max(CASE WHEN v.attribute = 'mpg_city' THEN v.effective_date END) AS mpg_city_date,
    max(CASE WHEN v.attribute = 'mpg_highway' THEN v.value_num END) AS mpg_highway,
    max(CASE WHEN v.attribute = 'mpg_highway' THEN v.claim_id END) AS mpg_highway_claim,
    max(CASE WHEN v.attribute = 'mpg_highway' THEN v.effective_date END) AS mpg_highway_date,
    max(CASE WHEN v.attribute = 'mpg_combined' THEN v.value_num END) AS mpg_combined,
    max(CASE WHEN v.attribute = 'mpg_combined' THEN v.claim_id END) AS mpg_combined_claim,
    max(CASE WHEN v.attribute = 'mpg_combined' THEN v.effective_date END) AS mpg_combined_date
FROM style_records AS r
LEFT JOIN style_values AS v ON v.style_key = r.style_key AND v.is_chosen
GROUP BY ALL;

-- Cells where the claims that lead for a style state different values.
-- `chosen_value` is empty when nothing could settle the cell.
CREATE OR REPLACE VIEW disagreements AS
SELECT
    v.style_key,
    r.model_year,
    r.description,
    v.attribute,
    count(DISTINCT v.value_num) AS value_count,
    list(DISTINCT v.value_num ORDER BY v.value_num) AS stated_values,
    max(CASE WHEN v.is_chosen THEN v.value_num END) AS chosen_value
FROM style_values AS v
JOIN style_records AS r ON r.style_key = v.style_key
WHERE v.is_conflicting
GROUP BY v.style_key, r.model_year, r.description, v.attribute;

-- Per model year and attribute: how many styles there are, how many have a
-- value, and how many were left open because sources disagree.
CREATE OR REPLACE VIEW coverage AS
WITH attributes AS (
    SELECT unnest([
        'msrp', 'delivery_fee', 'horsepower', 'torque', 'mpg_city', 'mpg_highway', 'mpg_combined'
    ]) AS attribute
),
cells AS (
    SELECT
        style_key,
        attribute,
        bool_or(is_chosen) AS is_filled,
        bool_or(is_conflicting) AS is_conflicting
    FROM style_values
    GROUP BY style_key, attribute
)
SELECT
    r.model_year,
    a.attribute,
    count(*) AS style_count,
    count(*) FILTER (WHERE c.is_filled) AS filled_count,
    count(*) FILTER (WHERE c.is_conflicting AND NOT c.is_filled) AS open_count,
    count(*) FILTER (WHERE c.style_key IS NULL) AS unstated_count
FROM style_records AS r
CROSS JOIN attributes AS a
LEFT JOIN cells AS c ON c.style_key = r.style_key AND c.attribute = a.attribute
GROUP BY r.model_year, a.attribute
ORDER BY r.model_year, a.attribute;
