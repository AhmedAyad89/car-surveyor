# OpenSpec's published schema

The schema files of OpenSpec release 2026-11-09, published by Visor. They
are copied here so that the reference loader can be written, and read by
an agent, without anyone opening OpenSpec's data.

- **What is here.** `structure.schema.yaml` (the shape of a model-year
  file), `lineup.schema.yaml`, `paint_palettes.schema.yaml`, and one
  file per attribute category under `attributes/`. They hold field
  names, types and descriptions, and no vehicle data.
- **What is not here.** OpenSpec's data. It stays outside the repository
  under `~/refdata/openspec/`, where only the diff reads it and only the
  user runs the diff. See [the isolation rules](../plan/isolation.md).
- **Licence.** OpenSpec publishes its schema and tooling under the
  Apache License 2.0, a copy of which is in
  [LICENSE-APACHE-2.0.txt](LICENSE-APACHE-2.0.txt). Its database is
  under a different licence, the ODbL, and none of it is here.
- **Changes.** None. The files are as they were in the release.

The loader that follows this schema is `src/car_surveyor/reference.py`,
and what it compares is in
[docs/plan/evaluation.md](../plan/evaluation.md#what-is-compared).
