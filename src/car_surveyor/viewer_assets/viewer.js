// The development viewer's page. Plain JavaScript, no build step, nothing from the network.
//
// Every view is drawn from JSON under /api/. What is on screen is in the URL
// hash (#tab?filters&panel=...), so a view can be reloaded or pasted. Text from
// documents is always inserted as text, never as markup.
"use strict";

const ATTRIBUTES = [
  "msrp",
  "delivery_fee",
  "horsepower",
  "torque",
  "mpg_city",
  "mpg_highway",
  "mpg_combined",
];
const ATTRIBUTE_NAMES = {
  msrp: "MSRP",
  delivery_fee: "Delivery fee",
  horsepower: "Horsepower",
  torque: "Torque",
  mpg_city: "City mpg",
  mpg_highway: "Highway mpg",
  mpg_combined: "Combined mpg",
};
const COLUMN_LABELS = {
  msrp: "MSRP",
  delivery_fee: "Delivery",
  horsepower: "hp",
  torque: "lb-ft",
  mpg_city: "City",
  mpg_highway: "Hwy",
  mpg_combined: "Comb",
};
const TABS = [
  ["catalogue", "Catalogue"],
  ["documents", "Documents"],
  ["claims", "Claims"],
  ["checks", "Checks"],
  ["diff", "Diff"],
];
// The diff's verdict on a cell. A null status is a cell the reference leaves empty.
const DIFF_STATUSES = {
  agree: { label: "agree", mark: "=" },
  disagree: { label: "disagree", mark: "≠" },
  pipeline_empty: { label: "pipeline empty", mark: "∅" },
  gap: { label: "reference empty", mark: "+" },
};
const PROBLEM_TITLES = {
  locked: "The pipeline is running",
  no_database: "No database yet",
  not_resolved: "Nothing resolved yet",
  unreachable: "The viewer is not answering",
  not_found: "Not found",
};
const MAX_DIFF_ROWS = 500;

const token = new URLSearchParams(location.search).get("token") || "";
const cache = new Map();
const view = { identity: null, filters: null, refresh: null, serial: 0 };
const panel = { spec: null, serial: 0 };
let meta = null;

const main = document.getElementById("main");
const panelElement = document.getElementById("panel");
const panelTitle = document.getElementById("panel-title");
const panelBody = document.getElementById("panel-body");

// --- building elements -------------------------------------------------------

function h(tag, attributes, ...children) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") element.className = value;
    else if (name === "dataset") Object.assign(element.dataset, value);
    else if (name.startsWith("on")) element.addEventListener(name.slice(2), value);
    else element.setAttribute(name, value === true ? "" : value);
  }
  append(element, children);
  return element;
}

function append(element, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

function buildTable(className, headers, rows) {
  return h(
    "table",
    { class: className },
    h("thead", null, h("tr", null, headers.map((header) => asHeaderCell(header)))),
    h("tbody", null, rows)
  );
}

function asHeaderCell(header) {
  if (header instanceof Node) return header;
  const [label, className] = Array.isArray(header) ? header : [header, null];
  return h("th", { scope: "col", class: className }, label);
}

function buildSelect(label, options) {
  const select = h("select", { "aria-label": label });
  setOptions(select, options);
  return select;
}

function setOptions(select, options) {
  const chosen = select.value;
  select.replaceChildren(
    ...options.map(([value, text]) => h("option", { value: String(value) }, text))
  );
  select.value = options.some(([value]) => String(value) === chosen) ? chosen : "";
}

function setValue(control, value) {
  if (control.value !== value) control.value = value;
}

function buildPanelButton(spec, label, className) {
  return h("button", { type: "button", class: className || "link", dataset: { panel: spec } }, label);
}

function buildFacts(pairs) {
  const list = h("dl", { class: "facts" });
  for (const [name, value] of pairs) {
    if (value === null || value === undefined || value === "") continue;
    list.append(h("div", null, h("dt", null, name), h("dd", null, value)));
  }
  return list;
}

function buildBadge(kind, text) {
  return h("span", { class: `badge badge-${kind}` }, text);
}

function buildEmpty(text) {
  return h("span", { class: "empty", title: text || "nothing" }, "—");
}

// --- formatting --------------------------------------------------------------

const wholeDollars = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});
const dollarsAndCents = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const plainNumber = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

function isNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function formatCount(value) {
  return isNumber(value) ? plainNumber.format(value) : "";
}

function formatMoney(value) {
  return Number.isInteger(value) ? wholeDollars.format(value) : dollarsAndCents.format(value);
}

function isMoney(attribute, unit) {
  return attribute === "msrp" || attribute === "delivery_fee" || unit === "USD";
}

// A value as the catalogue shows it: dollars for money, a bare number under a
// column that names the unit, and the unit spelled out where it is not the column's.
function formatValue(attribute, value, unit) {
  if (!isNumber(value)) return value === null || value === undefined ? "" : String(value);
  if (isMoney(attribute, unit)) return formatMoney(value);
  return plainNumber.format(value) + (unit === "MPGe" ? " MPGe" : "");
}

function formatWithUnit(attribute, value, unit) {
  const text = formatValue(attribute, value, unit);
  return isMoney(attribute, unit) || !unit || unit === "MPGe" ? text : `${text} ${unit}`;
}

function formatDate(text) {
  return text ? String(text).slice(0, 10) : "";
}

function formatDateTime(text) {
  return text ? String(text).slice(0, 16).replace("T", " ") : "";
}

function describeCell(attribute, cell) {
  if (!cell || cell.state === "unstated") return "unstated: no admitted claim is attached";
  if (cell.state === "open") {
    const values = cell.values.map((value) => formatValue(attribute, value, cell.unit));
    return `left open: the leading claims state ${values.join(" or ")}`;
  }
  const dated = cell.date ? `, as of ${formatDate(cell.date)}` : "";
  const contested = cell.is_contested ? "; another leading claim states a different value" : "";
  return `chosen: ${formatWithUnit(attribute, cell.value, cell.unit)}${dated}${contested}`;
}

// The inside of a catalogue cell, in one of its three states.
function buildCellContent(attribute, cell) {
  if (!cell || cell.state === "unstated") return buildEmpty("unstated");
  if (cell.state === "open") {
    const values = cell.values.map((value) => formatValue(attribute, value, cell.unit));
    return h("span", { class: "open-values" }, values.join(" or "));
  }
  return [
    formatValue(attribute, cell.value, cell.unit),
    cell.is_contested ? h("span", { class: "contested", "aria-label": "contested" }, "*") : null,
  ];
}

// --- talking to the server ---------------------------------------------------

class Problem extends Error {
  constructor(state, message) {
    super(message);
    this.state = state;
  }
}

async function request(path, parameters) {
  const query = new URLSearchParams(parameters || {});
  if (token) query.set("token", token);
  const text = query.toString();
  let response;
  try {
    response = await fetch(path + (text ? `?${text}` : ""));
  } catch (error) {
    throw new Problem("unreachable", "No answer from the server. Is `just view` still running?");
  }
  let payload;
  try {
    payload = await response.json();
  } catch (error) {
    throw new Problem("error", `The server answered ${response.status} with something unreadable.`);
  }
  if (!response.ok) {
    throw new Problem(payload.state || "error", payload.message || `HTTP ${response.status}`);
  }
  return payload;
}

// One request per endpoint and parameters until Reload is pressed. A failure is not kept.
function load(path, parameters) {
  const key = `${path}?${new URLSearchParams(parameters || {})}`;
  if (!cache.has(key)) {
    const pending = request(path, parameters);
    cache.set(key, pending);
    pending.catch(() => cache.delete(key));
  }
  return cache.get(key);
}

function getRun() {
  return readHash().params.get("run") || "";
}

function loadDiff() {
  const run = getRun();
  return load("/api/diff", run ? { run } : {});
}

// The catalogue and the panel are worth showing when the diff cannot be read.
function loadDiffQuietly() {
  return loadDiff().catch(() => null);
}

const diffIndexes = new WeakMap();

// The diff's cells by style and attribute, its extra styles, and, when the
// reference is being shown, the reference rows each style was matched to. A
// style can be matched more than once: by a model year's file and by its
// mid-year file.
function indexDiff(diff) {
  if (!diff || !diff.feedback) return null;
  if (!diffIndexes.has(diff)) {
    const cells = new Map();
    for (const cell of diff.feedback.cells) {
      if (!cells.has(cell.style_key)) cells.set(cell.style_key, new Map());
      cells.get(cell.style_key).set(cell.attribute, cell);
    }
    const matches = new Map();
    for (const match of (diff.reference && diff.reference.matches) || []) {
      if (!matches.has(match.style_key)) matches.set(match.style_key, []);
      matches.get(match.style_key).push(match);
    }
    const extra = new Set(diff.feedback.extra_styles);
    diffIndexes.set(diff, { cells, matches, extra });
  }
  return diffIndexes.get(diff);
}

function getStatusKey(cell) {
  return cell.status === null || cell.status === undefined ? "gap" : cell.status;
}

// --- the URL hash ------------------------------------------------------------

function readHash() {
  const [name, query] = location.hash.replace(/^#/, "").split("?");
  const tab = TABS.some(([known]) => known === name) ? name : "catalogue";
  return { tab, params: new URLSearchParams(query || "") };
}

function buildHash(tab, params) {
  const text = params.toString();
  return `#${tab}${text ? `?${text}` : ""}`;
}

// Change some of the hash's parameters. A filter replaces the history entry,
// so Back leaves the view; opening a panel adds one, so Back closes it.
function setParams(changes, options) {
  const { tab, params } = readHash();
  for (const [name, value] of Object.entries(changes)) {
    if (value === null || value === undefined || value === "") params.delete(name);
    else params.set(name, value);
  }
  const hash = buildHash(tab, params);
  if (options && options.replace) {
    history.replaceState(null, "", hash);
    route();
  } else if (hash !== location.hash) {
    location.hash = hash;
  }
}

function setFilter(name, value) {
  setParams({ [name]: value }, { replace: true });
}

async function route() {
  const { tab, params } = readHash();
  renderTabs(tab);
  const identity = `${tab}|${params.get("run") || ""}`;
  if (view.identity !== identity) {
    view.identity = identity;
    view.filters = null;
    view.refresh = null;
    const serial = ++view.serial;
    main.replaceChildren(h("p", { class: "muted" }, "Loading…"));
    try {
      const mounted = await VIEWS[tab]();
      if (serial !== view.serial) return;
      main.replaceChildren();
      append(main, mounted.children);
      main.scrollTo(0, 0);
      view.refresh = mounted.refresh;
    } catch (error) {
      if (serial !== view.serial) return;
      showProblem(main, error);
    }
  }
  // Opening or closing the panel leaves the view as it is, and the focus where it was.
  const current = readHash().params;
  const filters = new URLSearchParams(current);
  filters.delete("panel");
  if (view.refresh && view.filters !== filters.toString()) {
    view.filters = filters.toString();
    view.refresh(current);
  }
  syncPanel(current.get("panel"));
}

function renderTabs(active) {
  const run = getRun();
  const suffix = run ? `?${new URLSearchParams({ run })}` : "";
  document.getElementById("tabs").replaceChildren(
    ...TABS.map(([name, label]) =>
      h("a", { href: `#${name}${suffix}`, "aria-current": name === active ? "page" : null }, label)
    )
  );
}

function showProblem(container, error) {
  console.error(error);
  container.replaceChildren(
    h(
      "div",
      { class: "notice problem", role: "alert" },
      h("strong", null, PROBLEM_TITLES[error.state] || "Something went wrong"),
      h("p", null, error.message || String(error)),
      h("button", { type: "button", onclick: reload }, "Try again")
    )
  );
}

function reload() {
  cache.clear();
  view.identity = null;
  panel.spec = null;
  start();
}

// --- the catalogue -----------------------------------------------------------

const CATALOGUE_COLUMNS = [
  ["model_year", "Year", "num"],
  ["description", "Description", null],
  ["model_code", "Code", null],
  ["drive_class", "Drive", null],
  ["transmission_class", "Trans.", null],
  ...ATTRIBUTES.map((attribute) => [attribute, COLUMN_LABELS[attribute], "num"]),
];

async function mountCatalogue() {
  const [catalogue, diff] = await Promise.all([load("/api/catalogue"), loadDiffQuietly()]);
  const index = indexDiff(diff);
  const year = buildSelect("Model year", []);
  const model = buildSelect("Model", []);
  const search = h("input", {
    type: "search",
    placeholder: "Search description, code or key",
    "aria-label": "Search styles",
  });
  const count = h("span", { class: "muted" });
  year.addEventListener("change", () => setFilter("year", year.value));
  model.addEventListener("change", () => setFilter("model", model.value));
  search.addEventListener("input", () => setFilter("q", search.value));

  const headRow = h("tr");
  const body = h("tbody");
  const table = h("table", { class: "grid catalogue" }, h("thead", null, headRow), body);

  function refresh(params) {
    const chosenYear = params.get("year") || "";
    const inYear = catalogue.styles.filter(
      (style) => !chosenYear || String(style.model_year) === chosenYear
    );
    setOptions(year, [["", "All years"], ...catalogue.model_years.map((each) => [each, each])]);
    setValue(year, chosenYear);
    const models = [...new Set(inYear.map((style) => style.model))].sort();
    setOptions(model, [["", "All models"], ...models.map((each) => [each, each])]);
    setValue(model, models.includes(params.get("model")) ? params.get("model") : "");
    setValue(search, params.get("q") || "");

    const words = (params.get("q") || "").toLowerCase().split(/\s+/).filter(Boolean);
    const rows = inYear.filter((style) => {
      if (model.value && style.model !== model.value) return false;
      const text = [style.description, style.model_code, style.style_key, style.grade]
        .join(" ")
        .toLowerCase();
      return words.every((word) => text.includes(word));
    });
    const sort = params.get("sort") || "";
    sortStyles(rows, sort);
    count.textContent = `${formatCount(rows.length)} of ${formatCount(catalogue.styles.length)} styles`;
    headRow.replaceChildren(...CATALOGUE_COLUMNS.map((column) => buildSortHeader(column, sort)));
    body.replaceChildren(...rows.map((style) => buildStyleRow(style, index)));
    if (!rows.length) {
      const note = catalogue.styles.length ? "No style matches." : "The database holds no styles.";
      body.append(h("tr", null, h("td", { colspan: CATALOGUE_COLUMNS.length, class: "muted" }, note)));
    }
  }

  return {
    children: [
      h("div", { class: "controls" }, year, model, search, count),
      buildCatalogueLegend(diff),
      table,
    ],
    refresh,
  };
}

function buildSortHeader([name, label, className], sort) {
  const direction = sort === name ? "ascending" : sort === `-${name}` ? "descending" : null;
  const next = direction === null ? name : direction === "ascending" ? `-${name}` : "";
  const arrow = direction === "ascending" ? " ▲" : direction === "descending" ? " ▼" : "";
  return h(
    "th",
    { scope: "col", class: className, "aria-sort": direction },
    h(
      "button",
      {
        type: "button",
        class: "sort",
        title: `Sort by ${label}`,
        dataset: { sort: name },
        onclick: () => {
          setFilter("sort", next);
          // The header was drawn again: put the focus back on this column's button.
          const redrawn = main.querySelector(`button.sort[data-sort="${name}"]`);
          if (redrawn) redrawn.focus();
        },
      },
      label + arrow
    )
  );
}

function sortStyles(styles, sort) {
  if (!sort) return;
  const name = sort.replace(/^-/, "");
  const sign = sort.startsWith("-") ? -1 : 1;
  const read = (style) => {
    if (!ATTRIBUTES.includes(name)) return style[name];
    const cell = style.cells[name];
    return cell.state === "chosen" ? cell.value : null;
  };
  // Empty cells go last whichever way the column is sorted.
  styles.sort((left, right) => {
    const [a, b] = [read(left), read(right)];
    if (a === null || a === undefined) return b === null || b === undefined ? 0 : 1;
    if (b === null || b === undefined) return -1;
    if (isNumber(a) && isNumber(b)) return sign * (a - b);
    return sign * String(a).localeCompare(String(b));
  });
}

function buildStyleRow(style, index) {
  const diffCells = index ? index.cells.get(style.style_key) : null;
  const isExtra = Boolean(index && index.extra.has(style.style_key));
  const opener = buildPanelButton(`style.${style.style_key}`, style.description);
  opener.title = `${style.description}\n${style.style_key}`;
  const row = h(
    "tr",
    null,
    h("td", { class: "num" }, style.model_year),
    h(
      "td",
      { class: "description" },
      h(
        "div",
        { class: "clip" },
        opener,
        style.finding_count
          ? h(
              "span",
              { class: "tag tag-finding", title: `${style.finding_count} consistency findings` },
              `${style.finding_count} ⚑`
            )
          : null,
        isExtra
          ? h("span", { class: "tag tag-extra", title: "The reference has no row for this style" }, "extra")
          : null
      )
    ),
    h("td", { class: "mono" }, style.model_code || buildEmpty("no model code")),
    buildClassCell(style.drive_class, style.is_drive_inferred),
    buildClassCell(style.transmission_class, style.is_transmission_inferred)
  );
  for (const attribute of ATTRIBUTES) {
    const cell = style.cells[attribute];
    const diffCell = diffCells ? diffCells.get(attribute) : null;
    const statusKey = diffCell ? getStatusKey(diffCell) : null;
    let title = `${ATTRIBUTE_NAMES[attribute]} — ${describeCell(attribute, cell)}`;
    if (diffCell) {
      title += `\ndiff: ${DIFF_STATUSES[statusKey] ? DIFF_STATUSES[statusKey].label : statusKey}`;
      if (diffCell.bucket) title += `, bucket ${diffCell.bucket}`;
    }
    row.append(
      h(
        "td",
        { class: `num${statusKey ? ` diff diff-${statusKey}` : ""}` },
        h(
          "button",
          {
            type: "button",
            class: `cell cell-${cell.state}`,
            title,
            dataset: { panel: `cell.${style.style_key}.${attribute}` },
          },
          statusKey && DIFF_STATUSES[statusKey]
            ? h("span", { class: "diff-mark", "aria-hidden": "true" }, DIFF_STATUSES[statusKey].mark)
            : null,
          h("span", null, buildCellContent(attribute, cell))
        )
      )
    );
  }
  return row;
}

function buildClassCell(value, isInferred) {
  if (!value) return h("td", null, buildEmpty("not stated"));
  return h(
    "td",
    null,
    value,
    isInferred
      ? h("span", { class: "tag tag-inferred", title: "Inferred from the rows printed beside this one" }, "inferred")
      : null
  );
}

function buildCatalogueLegend(diff) {
  const legend = h(
    "p",
    { class: "legend" },
    h("span", { class: "legend-item" }, h("span", { class: "swatch" }, "$1,234"), " chosen"),
    h(
      "span",
      { class: "legend-item" },
      h("span", { class: "swatch open-values" }, "31 or 32"),
      " left open by disagreement"
    ),
    h("span", { class: "legend-item" }, h("span", { class: "swatch" }, buildEmpty()), " unstated"),
    h("span", { class: "legend-item" }, h("span", { class: "swatch" }, "28", h("span", { class: "contested" }, "*")), " chosen, but contested")
  );
  if (diff && diff.feedback) {
    legend.append(h("span", { class: "legend-item muted" }, `diff ${diff.run_id}:`));
    for (const [key, status] of Object.entries(DIFF_STATUSES)) {
      legend.append(
        h(
          "span",
          { class: "legend-item" },
          h("span", { class: `swatch diff diff-${key}` }, h("span", { class: "diff-mark" }, status.mark)),
          ` ${status.label}`
        )
      );
    }
  }
  return legend;
}

// --- documents ---------------------------------------------------------------

async function mountDocuments() {
  const data = await load("/api/documents");
  const documents = data.documents;
  // What `just status --missing` lists: a page that is gone is not a file to download.
  const unavailable = documents.filter(
    (held) => held.status === "unavailable" && held.kind !== "vehicle_page"
  );
  const kinds = [...new Set(documents.map((held) => held.kind))].sort();
  const statuses = [...new Set(documents.map((held) => held.status))].sort();
  const kind = buildSelect("Kind", [["", "All kinds"], ...kinds.map((each) => [each, each])]);
  const status = buildSelect("Status", [["", "All statuses"], ...statuses.map((each) => [each, each])]);
  const search = h("input", { type: "search", placeholder: "Search address", "aria-label": "Search documents" });
  const count = h("span", { class: "muted" });
  kind.addEventListener("change", () => setFilter("kind", kind.value));
  status.addEventListener("change", () => setFilter("status", status.value));
  search.addEventListener("input", () => setFilter("q", search.value));
  const body = h("tbody");

  function refresh(params) {
    setValue(kind, kinds.includes(params.get("kind")) ? params.get("kind") : "");
    setValue(status, statuses.includes(params.get("status")) ? params.get("status") : "");
    setValue(search, params.get("q") || "");
    const needle = search.value.toLowerCase();
    const rows = documents.filter(
      (held) =>
        (!kind.value || held.kind === kind.value) &&
        (!status.value || held.status === status.value) &&
        (!needle || held.url.toLowerCase().includes(needle))
    );
    count.textContent = `${formatCount(rows.length)} of ${formatCount(documents.length)} documents`;
    body.replaceChildren(...rows.map(buildDocumentRow));
    if (!rows.length) {
      body.append(h("tr", null, h("td", { colspan: 10, class: "muted" }, "No document matches.")));
    }
  }

  const summary = buildTable(
    "grid narrow",
    ["Kind", "Status", ["Documents", "num"]],
    data.counts.map((each) =>
      h(
        "tr",
        null,
        h("td", null, each.kind),
        h("td", null, buildBadge(`status-${each.status}`, each.status)),
        h("td", { class: "num" }, formatCount(each.count))
      )
    )
  );
  const headers = [
    "Status",
    "Kind",
    "Document",
    "Model years",
    "Route",
    "Captured",
    "Document date",
    "Fetched",
    "Claims",
    "Extraction",
  ];
  return {
    children: [
      buildUnavailableSection(unavailable, data.inbox_dir),
      h("h2", null, "By kind and status"),
      summary,
      h("h2", null, "Every document"),
      h("div", { class: "controls" }, kind, status, search, count),
      h("table", { class: "grid" }, h("thead", null, h("tr", null, headers.map(asHeaderCell))), body),
    ],
    refresh,
  };
}

function buildUnavailableSection(unavailable, inboxDir) {
  if (!unavailable.length) {
    return h("p", { class: "muted" }, "No document is waiting to be fetched by hand.");
  }
  return h(
    "section",
    { class: "attention" },
    h("h2", null, `To fetch by hand (${unavailable.length})`),
    h(
      "p",
      null,
      "No permitted client could get these. Download each in a browser, keep its file name, put it in ",
      h("code", null, `${inboxDir}/`),
      " and run ",
      h("code", null, "just snapshot"),
      "."
    ),
    buildTable(
      "grid",
      ["Save as", "Download from", "Model years", "Kind", "Why it is missing"],
      unavailable.map((held) => {
        const link = buildExternalLink(held.url, "address");
        link.title = held.url;
        return h(
          "tr",
          null,
          h("td", { class: "mono" }, held.file_name || held.url),
          h("td", null, link),
          h("td", null, held.model_years.join(", ") || buildEmpty("no model year stated")),
          h("td", null, held.kind),
          h("td", null, held.detail || buildEmpty())
        );
      })
    )
  );
}

function buildDocumentRow(held) {
  const snapshot = held.snapshot;
  const claims = Object.entries(held.claims)
    .sort()
    .map(([name, count]) => `${formatCount(count)} ${name}`)
    .join(", ");
  return h(
    "tr",
    { class: held.status === "unavailable" ? "row-unavailable" : null },
    h("td", null, buildBadge(`status-${held.status}`, held.status)),
    h("td", null, held.kind),
    h(
      "td",
      { class: "address" },
      h("span", { class: "mono" }, held.file_name || held.url),
      snapshot ? [" ", buildSnapshotLinks(snapshot.sha256, snapshot.is_pdf, null)] : null,
      " ",
      buildExternalLink(held.url, "address"),
      held.detail ? h("div", { class: "muted" }, held.detail) : null
    ),
    h("td", null, held.model_years.join(", ") || buildEmpty("no model year stated")),
    h("td", null, snapshot ? snapshot.route : buildEmpty("not fetched")),
    h("td", null, snapshot && snapshot.capture_date ? formatDate(snapshot.capture_date) : buildEmpty()),
    h("td", null, snapshot && snapshot.document_date ? formatDate(snapshot.document_date) : buildEmpty()),
    h("td", null, snapshot ? formatDateTime(snapshot.fetched_at) : buildEmpty("not fetched")),
    h("td", null, claims || buildEmpty("no claims")),
    h(
      "td",
      null,
      held.extractions.length
        ? held.extractions.map((each) =>
            h("div", null, h("span", { class: "mono" }, each.extractor_version), ` ${each.status}: ${each.detail}`)
          )
        : buildEmpty("not extracted")
    )
  );
}

function buildSnapshotLinks(sha256, isPdf, page) {
  const fragment = page ? `#page=${page}` : "";
  return [
    isPdf
      ? h(
          "a",
          { href: `/snapshot/${sha256}/raw${fragment}`, target: "_blank", rel: "noopener" },
          page ? `PDF, page ${page}` : "PDF"
        )
      : null,
    isPdf ? " " : null,
    h("a", { href: `/snapshot/${sha256}/text`, target: "_blank", rel: "noopener" }, "text"),
  ];
}

// A link out of the viewer, only for an address that is plainly a web address.
function buildExternalLink(url, label) {
  if (!/^https?:\/\//.test(url || "")) return h("span", { class: "mono" }, url || "");
  return h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, label);
}

// --- claims ------------------------------------------------------------------

async function mountClaims() {
  const data = await load("/api/claims");
  const statuses = [...new Set(data.counts.map((each) => each.status))].sort();
  const countOf = (type, status) => {
    const found = data.counts.find((each) => each.claim_type === type && each.status === status);
    return found ? found.claim_count : 0;
  };
  const byStatus = buildTable(
    "grid narrow",
    ["Status", ["Style claims", "num"], ["Value claims", "num"]],
    statuses.map((status) =>
      h(
        "tr",
        null,
        h("td", null, buildBadge(`status-${status}`, status)),
        h("td", { class: "num" }, formatCount(countOf("style", status))),
        h("td", { class: "num" }, formatCount(countOf("value", status)))
      )
    )
  );
  const reasons = [...new Set(data.reject_reasons.map((each) => each.reject_reason))];
  const reasonCount = (reason, type) =>
    data.reject_reasons
      .filter((each) => each.reject_reason === reason && (!type || each.claim_type === type))
      .reduce((total, each) => total + each.claim_count, 0);
  const byReason = buildTable(
    "grid narrow",
    ["Reject reason", ["Style claims", "num"], ["Value claims", "num"]],
    reasons.map((reason) =>
      h(
        "tr",
        null,
        h(
          "td",
          null,
          h("button", { type: "button", class: "link", onclick: () => setFilter("reason", reason) }, reason)
        ),
        h("td", { class: "num" }, formatCount(reasonCount(reason, "style"))),
        h("td", { class: "num" }, formatCount(reasonCount(reason, "value")))
      )
    )
  );
  const reason = buildSelect("Reject reason", [["", "All reasons"], ...reasons.map((each) => [each, each])]);
  const search = h("input", {
    type: "search",
    placeholder: "Search quote, value or document",
    "aria-label": "Search rejected claims",
  });
  const count = h("span", { class: "muted" });
  reason.addEventListener("change", () => setFilter("reason", reason.value));
  search.addEventListener("input", () => setFilter("q", search.value));
  const body = h("tbody");

  function refresh(params) {
    setValue(reason, reasons.includes(params.get("reason")) ? params.get("reason") : "");
    setValue(search, params.get("q") || "");
    const needle = search.value.toLowerCase();
    const rows = data.rejected.filter((each) => {
      if (reason.value && each.reject_reason !== reason.value) return false;
      const text = [each.quote, each.value_text, each.file_name, each.attribute, each.style_key]
        .join(" ")
        .toLowerCase();
      return !needle || text.includes(needle);
    });
    count.textContent =
      `${formatCount(rows.length)} of ${formatCount(data.rejected_count)} rejected claims` +
      (data.is_cut ? ` (the first ${formatCount(data.rejected.length)} are listed)` : "");
    body.replaceChildren(
      ...rows.map((each) =>
        h(
          "tr",
          null,
          h("td", null, each.claim_type),
          h("td", null, each.attribute ? ATTRIBUTE_NAMES[each.attribute] || each.attribute : buildEmpty()),
          h("td", null, buildPanelButton(`claim.${each.claim_id}`, each.value_text || "(no value)")),
          h("td", null, each.reject_reason),
          h("td", { class: "quote-cell" }, each.quote),
          h("td", { class: "mono" }, each.file_name || each.snapshot_sha256.slice(0, 12))
        )
      )
    );
    if (!rows.length) {
      const note = data.rejected_count ? "No rejected claim matches." : "No claim has been rejected.";
      body.append(h("tr", null, h("td", { colspan: 6, class: "muted" }, note)));
    }
  }

  return {
    children: [
      h("h2", null, "Claims by status"),
      statuses.length ? byStatus : h("p", { class: "muted" }, "The database holds no claims."),
      h("h2", null, "Rejected claims by reason"),
      reasons.length ? byReason : h("p", { class: "muted" }, "No claim has been rejected."),
      h("h2", null, "Rejected claims"),
      h("div", { class: "controls" }, reason, search, count),
      h(
        "table",
        { class: "grid" },
        h(
          "thead",
          null,
          h("tr", null, ["Claim", "Attribute", "Value", "Reason", "Quote", "Document"].map(asHeaderCell))
        ),
        body
      ),
    ],
    refresh,
  };
}

// --- checks ------------------------------------------------------------------

async function mountChecks() {
  const data = await load("/api/checks");
  const attributes = data.attributes || ATTRIBUTES;
  const years = [...new Set(data.coverage.map((each) => each.model_year))].sort();
  const coverageOf = (year, attribute) =>
    data.coverage.find((each) => each.model_year === year && each.attribute === attribute);
  const coverage = buildTable(
    "grid narrow coverage",
    [["Year", "num"], ["Styles", "num"], ...attributes.map((each) => [ATTRIBUTE_NAMES[each] || each, "num"])],
    years.map((year) =>
      h(
        "tr",
        null,
        h("td", { class: "num" }, year),
        h("td", { class: "num" }, formatCount((coverageOf(year, attributes[0]) || {}).style_count)),
        attributes.map((attribute) => {
          const cell = coverageOf(year, attribute);
          if (!cell) return h("td", { class: "num" }, buildEmpty());
          return h(
            "td",
            { class: "num", title: `${cell.filled_count} filled, ${cell.open_count} left open, ${cell.unstated_count} unstated` },
            h("span", { class: "filled" }, formatCount(cell.filled_count)),
            " / ",
            h("span", { class: cell.open_count ? "open-values" : "muted" }, formatCount(cell.open_count)),
            " / ",
            h("span", { class: "muted" }, formatCount(cell.unstated_count))
          );
        })
      )
    )
  );

  const findings = data.checks.map((check) =>
    h(
      "section",
      null,
      h("h3", null, h("span", { class: "mono" }, check.check_name), ` (${check.findings.length})`),
      check.findings.length
        ? buildTable(
            "grid",
            [["Year", "num"], "Style", "Attribute", "Finding"],
            check.findings.map((finding) =>
              h(
                "tr",
                null,
                h("td", { class: "num" }, finding.model_year || buildEmpty()),
                h(
                  "td",
                  null,
                  buildPanelButton(
                    finding.attribute
                      ? `cell.${finding.style_key}.${finding.attribute}`
                      : `style.${finding.style_key}`,
                    finding.description || finding.style_key
                  )
                ),
                h("td", null, finding.attribute ? ATTRIBUTE_NAMES[finding.attribute] || finding.attribute : buildEmpty()),
                h("td", null, finding.detail)
              )
            )
          )
        : h("p", { class: "muted" }, "No finding.")
    )
  );

  const outcome = buildSelect("Outcome", [
    ["", "Left open or not"],
    ["open", "Left open"],
    ["settled", "A value was chosen"],
  ]);
  const attribute = buildSelect("Attribute", [
    ["", "All attributes"],
    ...attributes.map((each) => [each, ATTRIBUTE_NAMES[each] || each]),
  ]);
  const count = h("span", { class: "muted" });
  outcome.addEventListener("change", () => setFilter("outcome", outcome.value));
  attribute.addEventListener("change", () => setFilter("attribute", attribute.value));
  const body = h("tbody");

  function refresh(params) {
    setValue(outcome, ["open", "settled"].includes(params.get("outcome")) ? params.get("outcome") : "");
    setValue(attribute, attributes.includes(params.get("attribute")) ? params.get("attribute") : "");
    const rows = data.disagreements.filter((each) => {
      const isOpen = each.chosen_value === null;
      if (outcome.value && (outcome.value === "open") !== isOpen) return false;
      return !attribute.value || each.attribute === attribute.value;
    });
    count.textContent = `${formatCount(rows.length)} of ${formatCount(data.disagreements.length)} cells`;
    body.replaceChildren(
      ...rows.map((each) =>
        h(
          "tr",
          null,
          h("td", { class: "num" }, each.model_year),
          h("td", null, buildPanelButton(`cell.${each.style_key}.${each.attribute}`, each.description)),
          h("td", null, ATTRIBUTE_NAMES[each.attribute] || each.attribute),
          h(
            "td",
            { class: "num" },
            each.stated_values.map((value) => formatValue(each.attribute, value, null)).join(" or ")
          ),
          h(
            "td",
            { class: "num" },
            each.chosen_value === null
              ? h("span", { class: "open-values" }, "left open")
              : formatValue(each.attribute, each.chosen_value, null)
          )
        )
      )
    );
    if (!rows.length) {
      body.append(h("tr", null, h("td", { colspan: 5, class: "muted" }, "No disagreement matches.")));
    }
  }

  return {
    children: [
      h("h2", null, "Coverage"),
      h("p", { class: "muted" }, "Per model year and attribute: styles filled / left open by disagreement / unstated."),
      years.length ? coverage : h("p", { class: "muted" }, "The database holds no styles."),
      h("h2", null, "Consistency findings"),
      h("p", { class: "muted" }, "A finding says where to look. It changes no value."),
      findings,
      h("h2", null, "Disagreements"),
      h("p", { class: "muted" }, "Cells whose leading claims state different values."),
      h("div", { class: "controls" }, outcome, attribute, count),
      h(
        "table",
        { class: "grid" },
        h(
          "thead",
          null,
          h("tr", null, [["Year", "num"], "Style", "Attribute", ["Stated", "num"], ["Chosen", "num"]].map(asHeaderCell))
        ),
        body
      ),
    ],
    refresh,
  };
}

// --- the diff ----------------------------------------------------------------

async function mountDiff() {
  const [diff, catalogue] = await Promise.all([loadDiff(), load("/api/catalogue").catch(() => null)]);
  const notes = [];
  if (diff.unreadable.length) {
    notes.push(h("p", { class: "notice" }, `Not readable as feedback: ${diff.unreadable.join(", ")}`));
  }
  if (!diff.feedback) {
    return {
      children: [
        ...notes,
        h(
          "div",
          { class: "notice" },
          h("strong", null, "No diff has been run for this data directory."),
          h(
            "p",
            null,
            "Run ",
            h("code", null, "just diff"),
            " from your own terminal. It writes a feedback file to ",
            h("code", null, `${diff.feedback_dir}/`),
            ", and this tab shows the latest one."
          )
        ),
      ],
      refresh: () => {},
    };
  }

  const feedback = diff.feedback;
  const index = indexDiff(diff);
  const styles = new Map(((catalogue && catalogue.styles) || []).map((style) => [style.style_key, style]));
  const hasReference = Boolean(diff.reference);
  if (diff.dropped_keys.length) {
    notes.push(
      h(
        "p",
        { class: "notice" },
        `Not shown, because the feedback contract does not name them: ${diff.dropped_keys.join(", ")}`
      )
    );
  }
  if (hasReference) {
    notes.push(
      h(
        "p",
        { class: "notice reference" },
        h("strong", null, "Showing reference values"),
        ` from ${diff.reference.file_name}. Keep this page away from any agent session.`
      )
    );
  } else if (meta && meta.reference === "shown" && diff.reference_run_id) {
    notes.push(
      h(
        "p",
        { class: "notice" },
        `The detail file describes run ${diff.reference_run_id}. This run is shown without reference values.`
      )
    );
  }

  const runPicker = buildSelect(
    "Diff run",
    diff.runs.map((run) => [run.run_id, `${run.run_id}${run.split ? ` · ${run.split}` : ""}`])
  );
  runPicker.value = diff.run_id;
  runPicker.addEventListener("change", () => setParams({ run: runPicker.value }));
  const tolerances = Object.entries(feedback.tolerances)
    .map(([name, value]) => `${ATTRIBUTE_NAMES[name] || name} ±${value}`)
    .join(", ");
  const header = h(
    "div",
    { class: "controls" },
    diff.runs.length > 1 ? h("label", null, "Run ", runPicker) : h("span", null, "Run ", h("span", { class: "mono" }, diff.run_id)),
    buildFacts([
      ["created", formatDateTime(feedback.created_at)],
      ["split", feedback.split],
      ["model years", feedback.model_years.join(", ")],
      ["commit", feedback.git_sha ? h("span", { class: "mono" }, feedback.git_sha) : null],
      ["tolerances", tolerances],
    ])
  );

  const matching = feedback.row_matching;
  const rowMatching = buildTable(
    "grid narrow",
    ["Row matching", ["Rows", "num"]],
    [
      ["Reference rows", matching.reference_rows],
      ["matched by model code", matching.matched_by_code],
      ["matched by description", matching.matched_by_description],
      ["missed by the pipeline", matching.missed],
      ["Pipeline styles", matching.pipeline_styles],
      ["extra (not in the reference)", matching.extra],
      ...Object.entries(matching).filter(
        ([name]) =>
          !["reference_rows", "matched_by_code", "matched_by_description", "missed", "pipeline_styles", "extra"].includes(name)
      ),
    ].map(([label, value]) =>
      h("tr", null, h("td", null, label), h("td", { class: "num" }, isNumber(value) ? formatCount(value) : buildEmpty("not in the file")))
    )
  );
  const buckets = buildTable(
    "grid narrow",
    ["Bucket", ["Cells", "num"]],
    Object.entries(feedback.buckets).map(([name, value]) =>
      h(
        "tr",
        null,
        h("td", null, h("button", { type: "button", class: "link", onclick: () => setFilter("bucket", name) }, name)),
        h("td", { class: "num" }, formatCount(value))
      )
    )
  );
  const countRows = (rows, keys) =>
    rows.map((row) =>
      h(
        "tr",
        null,
        keys.map((key, position) =>
          h(
            "td",
            { class: position === 1 ? null : "num" },
            row[key] === null || row[key] === undefined
              ? buildEmpty("not in the file")
              : isNumber(row[key]) && key !== "model_year"
                ? formatCount(row[key])
                : key === "attribute"
                  ? ATTRIBUTE_NAMES[row[key]] || row[key]
                  : row[key]
          )
        )
      )
    );
  const referenceModels = buildTable(
    "grid narrow",
    [["Year", "num"], "Reference model", ["Rows", "num"], ["Matched", "num"], ["Missed", "num"]],
    countRows(feedback.by_reference_model, ["model_year", "model", "rows", "matched", "missed"])
  );
  const pipelineModels = buildTable(
    "grid narrow",
    [["Year", "num"], "Pipeline model", ["Styles", "num"], ["Matched", "num"], ["Extra", "num"]],
    countRows(feedback.by_pipeline_model, ["model_year", "model", "styles", "matched", "extra"])
  );
  const metrics = buildTable(
    "grid narrow",
    [["Year", "num"], "Attribute", ["Agree", "num"], ["Disagree", "num"], ["Pipeline empty", "num"], ["Gap filled", "num"]],
    countRows(feedback.metrics, ["model_year", "attribute", "agree", "disagree", "pipeline_empty", "gap_filled"])
  );

  const statusKeys = [...new Set(feedback.cells.map(getStatusKey))];
  const bucketNames = [...new Set([...Object.keys(feedback.buckets), ...feedback.cells.map((cell) => cell.bucket).filter(Boolean)])];
  const cellAttributes = [...new Set(feedback.cells.map((cell) => cell.attribute))];
  const status = buildSelect("Status", [
    ["", "All statuses"],
    ...statusKeys.map((key) => [key, DIFF_STATUSES[key] ? DIFF_STATUSES[key].label : key]),
  ]);
  const bucket = buildSelect("Bucket", [["", "All buckets"], ...bucketNames.map((each) => [each, each])]);
  const attribute = buildSelect("Attribute", [
    ["", "All attributes"],
    ...cellAttributes.map((each) => [each, ATTRIBUTE_NAMES[each] || each]),
  ]);
  const search = h("input", { type: "search", placeholder: "Search style", "aria-label": "Search diff cells" });
  const count = h("span", { class: "muted" });
  status.addEventListener("change", () => setFilter("status", status.value));
  bucket.addEventListener("change", () => setFilter("bucket", bucket.value));
  attribute.addEventListener("change", () => setFilter("attribute", attribute.value));
  search.addEventListener("input", () => setFilter("q", search.value));
  const body = h("tbody");
  const more = h("p", { class: "muted" });
  const cellHeaders = [
    ["Year", "num"],
    "Style",
    "Attribute",
    "Status",
    "Bucket",
    [hasReference ? "Pipeline" : "Pipeline now", "num"],
    ...(hasReference ? [["Reference", "num"], "Matched by"] : []),
  ];

  function refresh(params) {
    setValue(status, statusKeys.includes(params.get("status")) ? params.get("status") : "");
    setValue(bucket, bucketNames.includes(params.get("bucket")) ? params.get("bucket") : "");
    setValue(attribute, cellAttributes.includes(params.get("attribute")) ? params.get("attribute") : "");
    setValue(search, params.get("q") || "");
    const needle = search.value.toLowerCase();
    const rows = feedback.cells.filter((cell) => {
      if (status.value && getStatusKey(cell) !== status.value) return false;
      if (bucket.value && cell.bucket !== bucket.value) return false;
      if (attribute.value && cell.attribute !== attribute.value) return false;
      if (!needle) return true;
      const style = styles.get(cell.style_key);
      return `${cell.style_key} ${style ? style.description : ""}`.toLowerCase().includes(needle);
    });
    const isShowingAll = params.get("all") === "1" || rows.length <= MAX_DIFF_ROWS;
    const shown = isShowingAll ? rows : rows.slice(0, MAX_DIFF_ROWS);
    count.textContent = `${formatCount(rows.length)} of ${formatCount(feedback.cells.length)} cells`;
    body.replaceChildren(...shown.map((cell) => buildDiffRow(cell, styles, index, hasReference)));
    if (!rows.length) {
      body.append(h("tr", null, h("td", { colspan: cellHeaders.length, class: "muted" }, "No cell matches.")));
    }
    more.replaceChildren();
    if (!isShowingAll) {
      more.append(
        `Showing the first ${formatCount(MAX_DIFF_ROWS)}. `,
        h("button", { type: "button", class: "link", onclick: () => setFilter("all", "1") }, "Show all")
      );
    }
  }

  const children = [
    ...notes,
    header,
    h("h2", null, "Row matching"),
    h("div", { class: "side-by-side" }, rowMatching, referenceModels, pipelineModels),
    h("h2", null, "Metrics"),
    h("div", { class: "side-by-side" }, metrics, buckets),
    h("h2", null, "Cells"),
    h("div", { class: "controls" }, status, bucket, attribute, search, count),
    h("table", { class: "grid" }, h("thead", null, h("tr", null, cellHeaders.map(asHeaderCell))), body),
    more,
    buildExtraStyles(diff, styles),
  ];
  if (hasReference) children.push(buildMatchSection(diff, styles), buildMissedSection(diff));
  return { children, refresh };
}

function buildDiffRow(cell, styles, index, hasReference) {
  const style = styles.get(cell.style_key);
  const statusKey = getStatusKey(cell);
  const known = DIFF_STATUSES[statusKey];
  const row = h(
    "tr",
    null,
    h("td", { class: "num" }, style ? style.model_year : buildEmpty()),
    h("td", null, buildPanelButton(`cell.${cell.style_key}.${cell.attribute}`, style ? style.description : cell.style_key)),
    h("td", null, ATTRIBUTE_NAMES[cell.attribute] || cell.attribute),
    h(
      "td",
      { class: `diff diff-${statusKey}` },
      known ? h("span", { class: "diff-mark", "aria-hidden": "true" }, known.mark) : null,
      " ",
      known ? known.label : statusKey
    ),
    h("td", null, cell.bucket || buildEmpty("no bucket"))
  );
  if (!hasReference) {
    const held = style ? style.cells[cell.attribute] : null;
    row.append(h("td", { class: "num" }, held ? buildCellContent(cell.attribute, held) : buildEmpty("the catalogue is not loaded")));
    return row;
  }
  // One line per reference row the style was matched to that has this cell.
  const compared = listComparisons(index, cell.style_key, cell.attribute);
  const lines = (build) => (compared.length ? compared.map((each) => h("div", null, build(each))) : buildEmpty("not compared"));
  row.append(
    h("td", { class: "num" }, lines(({ cell: matched }) => buildPipelineValues(cell.attribute, matched))),
    h("td", { class: "num reference-value" }, lines(({ cell: matched }) => buildReferenceValue(cell.attribute, matched))),
    h("td", null, lines(({ match }) => describeMatch(match)))
  );
  return row;
}

// Each reference row a style was matched to, with its side of one attribute.
function listComparisons(index, styleKey, attribute) {
  return (index.matches.get(styleKey) || [])
    .map((match) => ({ match, cell: (match.cells || []).find((each) => each.attribute === attribute) }))
    .filter((each) => each.cell);
}

function buildPipelineValues(attribute, matched) {
  if (matched.pipeline_value !== null && matched.pipeline_value !== undefined) {
    return formatValue(attribute, matched.pipeline_value, null);
  }
  const values = matched.pipeline_values || [];
  if (!values.length) return buildEmpty("the pipeline states none");
  return h("span", { class: "open-values" }, values.map((value) => formatValue(attribute, value, null)).join(" or "));
}

function buildReferenceValue(attribute, matched) {
  if (matched.reference_value === null || matched.reference_value === undefined) {
    return buildEmpty("the reference states none");
  }
  return formatValue(attribute, matched.reference_value, null);
}

// How a style was matched. A match made although the reference row lists
// other model codes than the style's says so.
function describeMatch(match) {
  const score = isNumber(match.score) ? ` (${plainNumber.format(match.score)})` : "";
  return `${match.matched_by || "?"}${score}${match.has_other_code ? ", codes differ" : ""}`;
}

function describeReferenceRow(reference) {
  if (!reference) return "";
  const codes = (reference.codes || []).join(", ");
  return [reference.file, reference.trim_name || reference.trim, reference.configuration, codes]
    .filter(Boolean)
    .join(" · ");
}

function buildExtraStyles(diff, styles) {
  const described = new Map(((diff.reference && diff.reference.extra) || []).map((each) => [each.style_key, each.description]));
  const keys = diff.feedback.extra_styles;
  if (!keys.length) return null;
  return h(
    "section",
    null,
    h("h2", null, `Pipeline styles the reference lacks (${keys.length})`),
    h(
      "ul",
      { class: "columns" },
      keys.map((key) => {
        const style = styles.get(key);
        return h("li", null, buildPanelButton(`style.${key}`, (style && style.description) || described.get(key) || key));
      })
    )
  );
}

function buildMatchSection(diff, styles) {
  const matches = diff.reference.matches;
  return h(
    "section",
    { class: "reference" },
    h("h2", null, `How each style was matched (${matches.length})`),
    buildTable(
      "grid",
      ["Pipeline style", "Matched by", ["Score", "num"], "Model codes", "Reference row"],
      matches.map((match) => {
        const style = styles.get(match.style_key);
        const description = (style && style.description) || match.description || match.style_key;
        return h(
          "tr",
          null,
          h("td", null, buildPanelButton(`style.${match.style_key}`, description)),
          h("td", null, match.matched_by || buildEmpty()),
          h("td", { class: "num" }, isNumber(match.score) ? plainNumber.format(match.score) : buildEmpty()),
          h(
            "td",
            null,
            match.has_other_code
              ? h("span", { class: "open-values", title: "The reference row lists other model codes than the style's" }, "differ")
              : buildEmpty("no difference")
          ),
          h("td", { class: "reference-value" }, describeReferenceRow(match.reference) || buildEmpty())
        );
      })
    )
  );
}

function buildMissedSection(diff) {
  const missed = diff.reference.missed;
  return h(
    "section",
    { class: "reference" },
    h("h2", null, `Reference styles the pipeline missed (${missed.length})`),
    missed.length
      ? buildTable(
          "grid",
          [["Year", "num"], "Model", "Trim", "Configuration", "Codes", "Labels", ["MSRP", "num"], "File"],
          missed.map((each) =>
            h(
              "tr",
              { class: "reference-value" },
              h("td", { class: "num" }, each.model_year || buildEmpty()),
              h("td", null, each.model || buildEmpty()),
              h("td", null, each.trim_name || each.trim || buildEmpty()),
              h("td", null, each.configuration || buildEmpty()),
              h("td", { class: "mono" }, (each.codes || []).join(", ") || buildEmpty()),
              h(
                "td",
                null,
                Object.entries(each.labels || {})
                  .map(([name, value]) => `${name}: ${value}`)
                  .join(", ") || buildEmpty()
              ),
              h("td", { class: "num" }, isNumber(each.msrp) ? formatMoney(each.msrp) : buildEmpty()),
              h("td", { class: "mono" }, each.file || buildEmpty())
            )
          )
        )
      : h("p", { class: "muted" }, "None.")
  );
}

// --- the panel ---------------------------------------------------------------

function parsePanel(spec) {
  const kind = spec.split(".")[0];
  const rest = spec.slice(kind.length + 1);
  if (kind !== "cell") return { kind, key: rest };
  const cut = rest.lastIndexOf(".");
  return { kind, key: rest.slice(0, cut), attribute: rest.slice(cut + 1) };
}

async function syncPanel(spec) {
  markSelected(spec);
  if (!spec) {
    panel.spec = null;
    panel.serial += 1;
    panelElement.hidden = true;
    document.body.classList.remove("has-panel");
    return;
  }
  if (spec === panel.spec) return;
  panel.spec = spec;
  const serial = ++panel.serial;
  panelElement.hidden = false;
  document.body.classList.add("has-panel");
  panelTitle.textContent = "Loading…";
  panelBody.replaceChildren();
  try {
    const { kind, key, attribute } = parsePanel(spec);
    if (!PANELS[kind]) throw new Problem("not_found", `There is no panel for ${spec}.`);
    const content = await PANELS[kind](key, attribute);
    if (serial !== panel.serial) return;
    panelTitle.textContent = content.title;
    panelBody.replaceChildren();
    append(panelBody, content.children);
    panelBody.scrollTop = 0;
    for (const row of panelBody.querySelectorAll(".located-row")) {
      const box = row.closest(".evidence-box");
      if (box) box.scrollTop = Math.max(0, row.offsetTop - box.clientHeight / 2);
    }
  } catch (error) {
    if (serial !== panel.serial) return;
    panelTitle.textContent = "Not available";
    showProblem(panelBody, error);
  }
}

function markSelected(spec) {
  for (const element of document.querySelectorAll(".selected")) element.classList.remove("selected");
  if (!spec) return;
  for (const element of main.querySelectorAll("[data-panel]")) {
    if (element.dataset.panel === spec) element.classList.add("selected");
  }
}

function closePanel() {
  const opener = main.querySelector(".selected");
  setParams({ panel: null });
  if (opener) opener.focus();
}

async function buildCellPanel(styleKey, attribute) {
  const [data, diff] = await Promise.all([
    load("/api/cell", { style_key: styleKey, attribute }),
    loadDiffQuietly(),
  ]);
  const style = data.style;
  return {
    title: `${style.description} · ${ATTRIBUTE_NAMES[attribute] || attribute}`,
    children: [
      buildStyleSummary(style),
      buildAttributeStrip(style, attribute),
      h("p", { class: `state state-${data.cell.state}` }, describeCell(attribute, data.cell)),
      buildDiffSection(diff, style.style_key, attribute),
      h("h3", null, `Values attached to this cell (${data.values.length})`),
      data.values.length
        ? data.values.map((value) => buildValueSection(attribute, value))
        : h("p", { class: "muted" }, "No admitted claim is attached. Nothing is filled in for it."),
      buildFindings(data.findings),
    ],
  };
}

async function buildStylePanel(styleKey) {
  const [data, diff] = await Promise.all([load("/api/style", { style_key: styleKey }), loadDiffQuietly()]);
  const style = data.style;
  return {
    title: style.description,
    children: [
      buildStyleSummary(style),
      buildFacts([
        ["model", style.model],
        ["grade", style.grade],
        ["powertrain", style.powertrain],
        ["body", style.body],
        ["engine", style.engine],
        ["transmission", style.transmission],
        ["drive", style.drive],
        ["documents", style.document_count],
      ]),
      buildAttributeStrip(style, null),
      buildDiffSection(diff, style.style_key, null),
      h("h3", null, `Rows that say this style exists (${data.claims.length})`),
      data.claims.map((claim) =>
        h(
          "section",
          { class: "claim" },
          h(
            "header",
            null,
            h("span", { class: "value" }, claim.value_text),
            buildBadge(`status-${claim.status}`, claim.status),
            claim.reject_reason ? h("span", { class: "problem-text" }, claim.reject_reason) : null
          ),
          buildClaim(claim)
        )
      ),
      buildFindings(data.findings),
    ],
  };
}

async function buildClaimPanel(claimId) {
  const data = await load("/api/claim", { claim_id: claimId });
  const claim = data.claim;
  const name = claim.attribute ? ATTRIBUTE_NAMES[claim.attribute] || claim.attribute : "Style";
  return {
    title: `${name} claim · ${claim.value_text || claim.claim_id}`,
    children: [
      h(
        "section",
        { class: "claim" },
        h(
          "header",
          null,
          h("span", { class: "value" }, claim.value_text),
          buildBadge(`status-${claim.status}`, claim.status),
          claim.reject_reason ? h("span", { class: "problem-text" }, claim.reject_reason) : null
        ),
        buildFacts([
          ["parsed", isNumber(data.value_num) ? formatWithUnit(claim.attribute, data.value_num, data.unit) : null],
          ["source", data.source_class],
          ["effective", formatDate(data.effective_date)],
          ["names the style", data.style_key ? buildPanelButton(`style.${data.style_key}`, data.style_key) : null],
        ]),
        buildClaim(claim)
      ),
      h("h3", null, `Cells it is attached to (${data.attached_to.length})`),
      data.attached_to.length
        ? h(
            "ul",
            { class: "plain" },
            data.attached_to.map((each) =>
              h(
                "li",
                null,
                buildPanelButton(`cell.${each.style_key}.${each.attribute}`, `${each.model_year} ${each.description}`),
                " ",
                each.is_chosen ? buildBadge("chosen", "chosen") : null,
                each.is_conflicting ? buildBadge("conflicting", "conflicting") : null
              )
            )
          )
        : h("p", { class: "muted" }, "None. Only an admitted claim that fits a style is attached."),
    ],
  };
}

function buildStyleSummary(style) {
  return h(
    "p",
    { class: "style-summary" },
    buildPanelButton(`style.${style.style_key}`, `${style.model_year} ${style.description}`),
    " ",
    h("span", { class: "mono muted" }, style.style_key),
    style.model_code ? [" · code ", h("span", { class: "mono" }, style.model_code)] : null,
    style.drive_class ? ` · ${style.drive_class}${style.is_drive_inferred ? " (inferred)" : ""}` : null,
    style.transmission_class
      ? ` · ${style.transmission_class}${style.is_transmission_inferred ? " (inferred)" : ""}`
      : null
  );
}

// Every attribute of the style, to move from one cell to the next.
function buildAttributeStrip(style, current) {
  return h(
    "div",
    { class: "strip" },
    ATTRIBUTES.map((attribute) =>
      h(
        "button",
        {
          type: "button",
          class: `strip-item cell-${style.cells[attribute].state}`,
          "aria-current": attribute === current ? "true" : null,
          dataset: { panel: `cell.${style.style_key}.${attribute}` },
        },
        h("span", { class: "muted" }, COLUMN_LABELS[attribute]),
        h("span", null, buildCellContent(attribute, style.cells[attribute]))
      )
    )
  );
}

function buildDiffSection(diff, styleKey, attribute) {
  const index = indexDiff(diff);
  if (!index) return null;
  const cell = attribute && index.cells.has(styleKey) ? index.cells.get(styleKey).get(attribute) : null;
  const matches = index.matches.get(styleKey) || [];
  const section = h(
    "section",
    { class: `diff-section${diff.reference ? " reference" : ""}` },
    h("h3", null, "Diff ", h("span", { class: "mono muted" }, diff.run_id), diff.reference ? buildBadge("reference", "showing reference values") : null)
  );
  if (index.extra.has(styleKey)) {
    section.append(h("p", null, "The reference has no row for this style."));
  } else if (attribute && !cell) {
    section.append(h("p", { class: "muted" }, "This cell is not in the run."));
  }
  if (cell) {
    const statusKey = getStatusKey(cell);
    const known = DIFF_STATUSES[statusKey];
    section.append(
      buildFacts([
        ["status", h("span", { class: `diff diff-${statusKey} diff-inline` }, known ? `${known.mark} ${known.label}` : statusKey)],
        ["bucket", cell.bucket],
      ])
    );
  }
  // One line of facts per reference row the style was matched to.
  for (const match of diff.reference ? matches : []) {
    const matched = attribute ? (match.cells || []).find((each) => each.attribute === attribute) : null;
    section.append(
      buildFacts([
        ["matched by", describeMatch(match)],
        ["reference row", h("span", { class: "reference-value" }, describeReferenceRow(match.reference))],
        matched ? ["pipeline", buildPipelineValues(attribute, matched)] : ["", null],
        matched
          ? ["reference", h("span", { class: "reference-value" }, buildReferenceValue(attribute, matched))]
          : ["", null],
      ])
    );
  }
  return section.children.length > 1 ? section : null;
}

function buildValueSection(attribute, value) {
  const isLeading = value.is_chosen || value.is_conflicting;
  return h(
    "section",
    { class: `claim${value.is_chosen ? " claim-chosen" : ""}` },
    h(
      "header",
      null,
      h("span", { class: "value" }, formatWithUnit(attribute, value.value_num, value.unit)),
      value.is_chosen ? buildBadge("chosen", "chosen") : null,
      value.is_conflicting ? buildBadge("conflicting", "conflicting") : null,
      isLeading ? null : buildBadge("outranked", "not leading"),
      h("span", { class: "muted" }, `printed as “${value.value_text}”`)
    ),
    buildFacts([
      ["source", value.source_class],
      ["specificity", value.specificity],
      ["note", value.note],
      ["effective", formatDate(value.effective_date) || "undated"],
      ["unit", value.unit],
    ]),
    buildClaim(value.claim)
  );
}

// A claim's own words: who extracted it, what it applies to, its quote, and the
// place in the document the quote was found.
function buildClaim(claim) {
  const scope = Object.entries(claim.applies_to || {})
    .filter(([, value]) => value !== null && value !== "" && !(Array.isArray(value) && !value.length))
    .map(([name, value]) => `${name}: ${Array.isArray(value) ? value.join(", ") : value}`);
  const qualifiers = Object.entries(claim.qualifiers || {}).map(([name, value]) => `${name}: ${value}`);
  return [
    buildFacts([
      ["claim", buildPanelButton(`claim.${claim.claim_id}`, claim.claim_id, "link mono")],
      ["extractor", h("span", { class: "mono" }, claim.extractor_version)],
      ["status", claim.status],
      ["applies to", scope.join(" · ")],
      ["qualifiers", qualifiers.join(" · ")],
      ["hedges", (claim.hedge_flags || []).join(" · ")],
    ]),
    h("blockquote", { class: "quote" }, claim.quote),
    buildEvidence(claim.evidence),
    buildDocumentLine(claim.document, claim.evidence),
  ];
}

function describeLocator(evidence) {
  if (evidence.kind === "table_cell") {
    return `table cell · page ${evidence.page}, table ${evidence.table} · row “${evidence.row_label}” · column “${evidence.column_header}”`;
  }
  if (evidence.kind === "file_row") return `file row · key ${evidence.row_key} · column ${evidence.column}`;
  if (evidence.kind === "text_span") return `text span · characters ${evidence.start}–${evidence.end} · page ${evidence.page || "?"}`;
  if (evidence.kind === "page_hint") {
    return `page hint${evidence.page ? ` · page ${evidence.page}` : ""}${evidence.quoted !== undefined ? " · found by searching for the quote" : ""}`;
  }
  return "locator not understood";
}

function buildEvidence(evidence) {
  const box = h("div", { class: "evidence-box" });
  if (evidence.kind === "table_cell" && evidence.rows) append(box, [buildEvidenceTable(evidence)]);
  else if (evidence.quoted !== undefined) append(box, [buildTextContext(evidence)]);
  else if (evidence.kind === "file_row" && evidence.fields) append(box, [buildRowFields(evidence)]);
  return h(
    "div",
    { class: "evidence" },
    h("p", { class: "muted evidence-where" }, describeLocator(evidence)),
    evidence.problem ? h("p", { class: "problem-text" }, `Not shown in place: ${evidence.problem}.`) : null,
    box.children.length ? box : null
  );
}

function buildEvidenceTable(evidence) {
  const isLocated = evidence.row_index !== undefined && evidence.row_index !== null;
  const table = h("table", { class: "evidence-table" });
  if (isLocated && evidence.header) {
    table.append(
      h(
        "thead",
        null,
        h(
          "tr",
          null,
          evidence.header.map((text, column) =>
            h("th", { scope: "col", class: column === evidence.column_index ? "located-column" : null }, text)
          )
        )
      )
    );
    if (evidence.header_row_index === null) {
      table.append(h("caption", null, "The header is from the table this one continues."));
    }
  }
  const body = h("tbody");
  evidence.rows.forEach((row, rowIndex) => {
    if (isLocated && rowIndex === evidence.header_row_index) return;
    const isLocatedRow = isLocated && rowIndex === evidence.row_index;
    body.append(
      h(
        "tr",
        { class: isLocatedRow ? "located-row" : null },
        row.map((text, column) => {
          if (!isLocatedRow || column !== evidence.column_index) return h("td", null, text);
          return h("td", { class: "located-cell" }, markSpan(text, evidence.quote_span));
        })
      )
    );
  });
  table.append(body);
  return table;
}

function markSpan(text, span) {
  if (!span) return text;
  return [text.slice(0, span[0]), h("mark", null, text.slice(span[0], span[1])), text.slice(span[1])];
}

function buildTextContext(evidence) {
  return h(
    "pre",
    { class: "context" },
    evidence.is_cut_before ? "… " : null,
    showPageBreaks(evidence.before),
    h("mark", null, evidence.quoted),
    showPageBreaks(evidence.after),
    evidence.is_cut_after ? " …" : null
  );
}

// Pages are joined with a form feed in a snapshot's text.
function showPageBreaks(text) {
  const pages = text.split("\f");
  return pages.flatMap((page, position) =>
    position === 0 ? [page] : [h("span", { class: "page-break" }, "page break"), page]
  );
}

function buildRowFields(evidence) {
  const describe = ([name, value]) =>
    h(
      "div",
      { class: name === evidence.column ? "located-field" : null },
      h("dt", null, name),
      h("dd", null, value === "" ? buildEmpty("empty in the file") : value)
    );
  return [
    h("dl", { class: "fields" }, evidence.fields.map(describe)),
    evidence.other_fields.length
      ? h(
          "details",
          null,
          h("summary", null, `The row's other ${evidence.other_fields.length} fields`),
          h("dl", { class: "fields" }, evidence.other_fields.map(describe))
        )
      : null,
  ];
}

function buildDocumentLine(held, evidence) {
  if (!held.is_held) {
    return h("p", { class: "document muted" }, "The pipeline holds no snapshot of this claim's document.");
  }
  return h(
    "p",
    { class: "document" },
    h("span", { class: "mono" }, held.file_name || held.url),
    " ",
    buildSnapshotLinks(held.sha256, held.is_pdf, held.is_pdf ? evidence.page : null),
    " ",
    buildExternalLink(held.url, "address"),
    h(
      "span",
      { class: "muted" },
      ` · ${held.kind}`,
      held.document_date ? ` · dated ${formatDate(held.document_date)}` : " · undated",
      held.capture_date ? ` · captured ${formatDate(held.capture_date)}` : "",
      ` · ${held.route} · fetched ${formatDate(held.fetched_at)}`,
      held.address_count > 1 ? ` · the same bytes are held under ${held.address_count} addresses` : ""
    )
  );
}

function buildFindings(findings) {
  return [
    h("h3", null, `Consistency findings for this style (${findings.length})`),
    findings.length
      ? h(
          "ul",
          { class: "plain" },
          findings.map((finding) =>
            h(
              "li",
              null,
              h("span", { class: "mono" }, finding.check_name),
              finding.attribute ? ` · ${ATTRIBUTE_NAMES[finding.attribute] || finding.attribute}` : "",
              ` — ${finding.detail}`
            )
          )
        )
      : h("p", { class: "muted" }, "None."),
  ];
}

const VIEWS = {
  catalogue: mountCatalogue,
  documents: mountDocuments,
  claims: mountClaims,
  checks: mountChecks,
  diff: mountDiff,
};
const PANELS = { cell: buildCellPanel, style: buildStylePanel, claim: buildClaimPanel };

// --- keys, clicks and starting -----------------------------------------------

// The arrow keys move between the buttons of a table, as in a spreadsheet.
function moveInGrid(button, key) {
  const cell = button.closest("td, th");
  if (!cell || !cell.closest("table.grid")) return false;
  const isBackward = key === "ArrowLeft" || key === "ArrowUp";
  if (key === "ArrowLeft" || key === "ArrowRight") {
    let sibling = cell;
    while ((sibling = isBackward ? sibling.previousElementSibling : sibling.nextElementSibling)) {
      const target = sibling.querySelector("button");
      if (target) return target.focus(), true;
    }
    return false;
  }
  let row = cell.parentElement;
  while ((row = isBackward ? row.previousElementSibling : row.nextElementSibling)) {
    const below = row.children[cell.cellIndex];
    const target = below ? below.querySelector("button") : null;
    if (target) return target.focus(), true;
  }
  return false;
}

document.addEventListener("click", (event) => {
  const opener = event.target.closest("[data-panel]");
  if (opener) setParams({ panel: opener.dataset.panel });
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && readHash().params.has("panel")) {
    event.preventDefault();
    closePanel();
    return;
  }
  if (event.key.startsWith("Arrow") && event.target instanceof HTMLButtonElement) {
    if (moveInGrid(event.target, event.key)) event.preventDefault();
  }
});

document.getElementById("panel-close").addEventListener("click", closePanel);
document.getElementById("reload").addEventListener("click", reload);
window.addEventListener("hashchange", route);

async function start() {
  try {
    meta = await request("/api/meta");
    // The end of the path says which data directory this is; the tooltip has all of it.
    const dataDir = document.getElementById("data-dir");
    dataDir.textContent = meta.data_dir.split("/").slice(-2).join("/");
    dataDir.title = meta.data_dir;
    document.getElementById("reference-badge").hidden = meta.reference !== "shown";
    document.body.classList.toggle("showing-reference", meta.reference === "shown");
    const note = document.getElementById("reference-note");
    note.hidden = meta.reference !== "needs_token";
    note.textContent =
      "This viewer was started with --diff-detail, but this address does not carry its token, " +
      "so no reference value is shown. Open the address printed in the terminal.";
  } catch (error) {
    showProblem(main, error);
    return;
  }
  route();
}

start();
