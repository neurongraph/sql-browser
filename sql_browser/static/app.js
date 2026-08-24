"use strict";

const $ = (id) => document.getElementById(id);

const els = {
  schemaTree: $("schema-tree"),
  askForm: $("ask-form"),
  nlInput: $("nl-input"),
  askBtn: $("ask-btn"),
  suggestions: $("suggestions"),
  history: $("history"),
  turnTemplate: $("turn-template"),
  fileSelect: $("file-select"),
  fileInput: $("file-input"),
  uploadBtn: $("upload-btn"),
  modelSelect: $("model-select"),
  loadBtn: $("load-btn"),
  toolbarStatus: $("toolbar-status"),
  proseFieldTemplate: $("prose-field-template"),
  metaReview: $("metadata-review"),
  mrSheets: $("mr-sheets"),
  mrEditor: $("mr-editor"),
  mrRecon: $("mr-recon"),
  mrDiscard: $("mr-discard"),
  mrPreview: $("mr-preview"),
  mrSave: $("mr-save"),
  metaEditor: $("metadata-editor"),
  metaToggle: $("metadata-toggle"),
  metaBody: $("metadata-body"),
  metaHint: $("metadata-hint"),
  metaProse: $("metadata-prose"),
  metaRecon: $("metadata-recon"),
  metaPreviewBtn: $("metadata-preview"),
  metaSaveBtn: $("metadata-save"),
};

// Mirrors the server's bound file/model so we can tell a real change from a
// no-op and avoid reloading the schema when only the model was switched.
const state = { file: null, model: null, ready: false, maxUploadMb: 0 };

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

function show(el) { el.classList.remove("hidden"); }
function hide(el) { el.classList.add("hidden"); }

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

// --------------------------------------------------------------------------- //
// Toolbar: file + model selectors
// --------------------------------------------------------------------------- //

function setToolbarStatus(message, isError = false, spinner = false) {
  els.toolbarStatus.className = "toolbar-status" + (isError ? " error" : "");
  els.toolbarStatus.innerHTML =
    (spinner ? '<span class="spinner"></span>' : "") + escapeHtml(message || "");
}

/** Populate a <select> from plain strings or {value, label} entries. */
function fillSelect(select, entries, selected, placeholder) {
  select.innerHTML = "";
  if (placeholder) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = placeholder;
    select.appendChild(option);
  }
  for (const entry of entries) {
    const option = document.createElement("option");
    option.value = typeof entry === "string" ? entry : entry.value;
    option.textContent = typeof entry === "string" ? entry : entry.label;
    select.appendChild(option);
  }
  select.value = selected || "";
}

/** Reflect "is a database loaded?" in the ask form. */
function setReady(ready) {
  state.ready = ready;
  els.nlInput.disabled = !ready;
  els.askBtn.disabled = !ready;
  els.nlInput.placeholder = ready
    ? "Ask a question about your data…"
    : "Upload or pick a file to begin…";
}

/** Fetch the server's current file/model plus the available options. */
async function loadConfig() {
  let data;
  try {
    data = await (await fetch("/api/config")).json();
  } catch (e) {
    setToolbarStatus("Could not reach the server.", true);
    return;
  }
  applyConfig(data);
  if (state.ready) {
    loadSchema();
    loadSuggestions();
    loadMetadata();
    maybeShowMetadataReview(data);
  } else {
    els.schemaTree.textContent = "No file loaded.";
    hide(els.metaEditor);
  }
}

function applyConfig(data) {
  state.file = data.file || null;
  state.model = data.model || null;
  state.maxUploadMb = data.max_upload_mb || 0;
  fillSelect(els.fileSelect, data.files || [], data.file, "— no file —");
  fillSelect(els.modelSelect, data.models || [], data.model);
  setReady(Boolean(data.ready));
}

/** Bind the selected file and/or model on the server.
 *
 * `force` re-opens and re-profiles the file even when it hasn't changed - what
 * the Load button does, so a database edited on disk can be picked up.
 */
async function loadSession(force = false) {
  const file = els.fileSelect.value;
  const model = els.modelSelect.value;
  if (!force && !file && model === state.model) return;

  const fileChanging = force ? Boolean(file) : file && file !== state.file;
  els.loadBtn.disabled = true;
  setToolbarStatus(fileChanging ? "Loading & profiling…" : "Switching model…", false, true);

  try {
    const data = await postJSON("/api/session", { file: file || null, model, force });
    applyConfig(data);
    if (data.error) {
      setToolbarStatus(data.error, true);
      return;
    }
    setToolbarStatus("");

    if (data.file_changed) {
      // A different database makes past turns and suggestions misleading.
      els.history.innerHTML = "";
      els.suggestions.innerHTML = "";
      els.schemaTree.textContent = "Loading…";
      await loadSchema();
      loadSuggestions();
      loadMetadata();
      maybeShowMetadataReview(data);
    }
  } catch (e) {
    setToolbarStatus("Request failed: " + e.message, true);
  } finally {
    els.loadBtn.disabled = false;
  }
}

/** Upload a file into the server's data directory, then load it. */
async function uploadFile(file) {
  const body = new FormData();
  body.append("file", file);

  els.uploadBtn.disabled = true;
  setToolbarStatus(`Uploading ${file.name}…`, false, true);

  try {
    const res = await fetch("/api/upload", { method: "POST", body });
    const data = await res.json();
    if (data.error) {
      setToolbarStatus(data.error, true);
      return;
    }
    // Stored under a possibly de-duplicated name; select that and load it.
    fillSelect(els.fileSelect, data.files || [], data.uploaded, "— no file —");
    await loadSession();
  } catch (e) {
    setToolbarStatus("Upload failed: " + e.message, true);
  } finally {
    els.uploadBtn.disabled = false;
    els.fileInput.value = "";
  }
}

// --------------------------------------------------------------------------- //
// Schema sidebar
// --------------------------------------------------------------------------- //

async function loadSchema() {
  try {
    const data = await (await fetch("/api/schema")).json();
    if (data.error) {
      els.schemaTree.textContent = data.error;
      return;
    }
    renderSchema(data.tables || [], data.relationships || [], data.metadata_warnings || []);
  } catch (e) {
    els.schemaTree.textContent = "Failed to load schema.";
  }
}

function renderSchema(tables, relationships, metadataWarnings) {
  els.schemaTree.innerHTML = "";
  for (const table of tables) {
    const wrap = document.createElement("div");
    wrap.className = "schema-table";

    const name = document.createElement("div");
    name.className = "tbl-name";
    name.innerHTML =
      escapeHtml(table.name) +
      ` <span class="row-count">(${table.row_count})</span>`;

    const cols = document.createElement("ul");
    cols.className = "schema-cols";
    cols.style.display = "none";

    // Curated descriptions, when a metadata file supplied them.
    if (table.description || table.grain) {
      const note = document.createElement("li");
      note.className = "tbl-note";
      note.textContent = table.grain
        ? `${table.description || ""} (one row per ${table.grain})`.trim()
        : table.description;
      cols.appendChild(note);
    }

    for (const col of table.columns) {
      const li = document.createElement("li");
      const profile = table.profile && table.profile[col.name];
      let hint = "";
      if (profile && profile.type === "enum") {
        hint = ` <span class="enum">${escapeHtml(profile.values.slice(0, 6).join(", "))}</span>`;
      } else if (profile && profile.type === "range") {
        hint = ` <span class="enum">${escapeHtml(profile.min)}–${escapeHtml(profile.max)}</span>`;
      }
      li.innerHTML =
        escapeHtml(col.name) +
        ` <span class="col-type">${escapeHtml(col.type)}</span>` +
        (col.pk ? ' <span class="pk">PK</span>' : "") +
        hint +
        (col.description
          ? ` <span class="col-desc">${escapeHtml(col.description)}</span>`
          : "");
      cols.appendChild(li);
    }

    name.addEventListener("click", () => {
      cols.style.display = cols.style.display === "none" ? "block" : "none";
    });

    wrap.appendChild(name);
    wrap.appendChild(cols);
    els.schemaTree.appendChild(wrap);
  }

  renderSchemaExtras(relationships, metadataWarnings);
}

/**
 * Append the join graph and any metadata problems below the table list.
 *
 * Both are collapsed by default: they matter when a query joins wrongly or a
 * metadata file has drifted, not on every glance at the schema.
 */
function renderSchemaExtras(relationships, metadataWarnings) {
  const sections = [
    {
      title: "Relationships",
      className: "schema-rels",
      items: relationships.map(
        (r) => (r.label || `${r.from} → ${r.to}`) + (r.composite ? "  (composite key)" : ""),
      ),
    },
    { title: "Metadata warnings", className: "schema-warnings", items: metadataWarnings },
  ];

  for (const section of sections) {
    if (!section.items.length) continue;

    const wrap = document.createElement("div");
    wrap.className = "schema-table";

    const heading = document.createElement("div");
    heading.className = "tbl-name";
    heading.innerHTML =
      escapeHtml(section.title) +
      ` <span class="row-count">(${section.items.length})</span>`;

    const list = document.createElement("ul");
    list.className = `schema-cols ${section.className}`;
    list.style.display = "none";
    for (const item of section.items) {
      const li = document.createElement("li");
      li.textContent = item;
      list.appendChild(li);
    }

    heading.addEventListener("click", () => {
      list.style.display = list.style.display === "none" ? "block" : "none";
    });

    wrap.appendChild(heading);
    wrap.appendChild(list);
    els.schemaTree.appendChild(wrap);
  }
}

async function loadSuggestions() {
  try {
    const data = await (await fetch("/api/suggestions")).json();
    for (const suggestion of data.suggestions || []) {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = suggestion;
      chip.addEventListener("click", () => {
        els.nlInput.value = suggestion;
        askQuery();
      });
      els.suggestions.appendChild(chip);
    }
  } catch (e) {
    /* suggestions are optional */
  }
}

// --------------------------------------------------------------------------- //
// Prose metadata editor (shared component)
//
// A free-form editor over the RAW prose document: one textarea for the database,
// one per real table, plus relationships and examples. Rendered here for the
// Excel review modal; B1 mounts the same component in the sidebar. It binds to
// `doc.prose` and reads back a `{database, tables, relationships, examples}`
// object — never the reconciled projection.
// --------------------------------------------------------------------------- //

const PROSE_FIELDS = {
  database: "What this data is; conventions, units, rules the model should know…",
  relationships: "How the tables join, in plain words…",
  examples: "Example questions worth teaching the model…",
};

function addProseField(container, key, labelText, value, placeholder) {
  const node = els.proseFieldTemplate.content.firstElementChild.cloneNode(true);
  node.querySelector(".prose-label").textContent = labelText;
  const textarea = node.querySelector(".prose-input");
  textarea.value = value || "";
  textarea.placeholder = placeholder || "";
  textarea.dataset.key = key;
  container.appendChild(node);
}

/** Fill `container` with prose fields seeded from `doc` (its `prose` + `tables`). */
function renderProseEditor(container, doc) {
  container.innerHTML = "";
  const prose = doc.prose || {};
  const tables = prose.tables || {};

  addProseField(container, "database", "Database", prose.database, PROSE_FIELDS.database);
  for (const table of doc.tables || []) {
    addProseField(
      container,
      "tables:" + table,
      "Table: " + table,
      tables[table],
      "What a row is; column meanings; code lookups (e.g. FPO = Faster Payment Out)…",
    );
  }
  addProseField(container, "relationships", "Relationships", prose.relationships, PROSE_FIELDS.relationships);
  addProseField(container, "examples", "Examples", prose.examples, PROSE_FIELDS.examples);
}

/** Read every textarea in `container` back into a prose object. */
function collectProse(container) {
  const prose = { database: "", tables: {}, relationships: "", examples: "" };
  container.querySelectorAll(".prose-input").forEach((textarea) => {
    const key = textarea.dataset.key;
    if (key.startsWith("tables:")) {
      prose.tables[key.slice("tables:".length)] = textarea.value;
    } else {
      prose[key] = textarea.value;
    }
  });
  return prose;
}

/** Render a "N kept / M dropped" line plus any reconcile warnings into `el`. */
function renderReconSummary(el, summary) {
  if (!summary) { el.innerHTML = ""; return; }
  if (summary.message) {
    el.innerHTML = `<div class="recon-counts">${escapeHtml(summary.message)}</div>`;
    return;
  }

  const parts = [`<span class="recon-ok">${summary.matched} item(s) kept</span>`];
  if (summary.dropped) {
    parts.push(`<span class="recon-drop">${summary.dropped} dropped</span>`);
  }
  let html = `<div class="recon-counts">${parts.join(" · ")}</div>`;
  if (summary.warnings && summary.warnings.length) {
    html +=
      '<ul class="recon-warnings">' +
      summary.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("") +
      "</ul>";
  }
  el.innerHTML = html;
}

// --------------------------------------------------------------------------- //
// Excel metadata review modal
// --------------------------------------------------------------------------- //

/** Offer to convert a workbook's documentation sheet into a sidecar. */
async function maybeShowMetadataReview(config) {
  if (!config || !config.metadata_suggestion) return;

  let doc;
  try {
    doc = await (await fetch("/api/metadata")).json();
  } catch (e) {
    return; // an offer we can't render is silently dropped, not fatal
  }
  if (doc.error || !doc.pending) return;

  els.mrSheets.textContent = (doc.pending.sheets || []).join(", ");
  renderProseEditor(els.mrEditor, doc);
  renderReconSummary(els.mrRecon, null);
  show(els.metaReview);
}

/** Dry-run reconcile: POST /preview and show the kept/dropped summary. */
async function previewProse(editorEl, reconEl, btn) {
  const prose = collectProse(editorEl);
  btn.disabled = true;
  renderReconSummary(reconEl, { message: "Generating & reconciling…" });
  try {
    const data = await postJSON("/api/metadata/preview", { prose });
    renderReconSummary(reconEl, data.error ? { message: data.error } : data);
  } catch (e) {
    renderReconSummary(reconEl, { message: "Preview failed: " + e.message });
  } finally {
    btn.disabled = false;
  }
}

/** Commit: POST the prose, returning the response (or null on failure). */
async function saveProse(editorEl, reconEl, btn) {
  const prose = collectProse(editorEl);
  btn.disabled = true;
  renderReconSummary(reconEl, { message: "Saving…" });
  try {
    const data = await postJSON("/api/metadata", { prose });
    if (data.error) {
      renderReconSummary(reconEl, { message: data.error });
      return null;
    }
    renderReconSummary(reconEl, data);
    return data;
  } catch (e) {
    renderReconSummary(reconEl, { message: "Save failed: " + e.message });
    return null;
  } finally {
    btn.disabled = false;
  }
}

async function saveMetadataReview() {
  const data = await saveProse(els.mrEditor, els.mrRecon, els.mrSave);
  if (!data) return;
  hide(els.metaReview);
  // The sidecar now feeds the schema view and the sidebar editor.
  await loadSchema();
  applyConfig(await (await fetch("/api/config")).json());
  loadMetadata();
}

async function discardMetadataReview() {
  try {
    await fetch("/api/metadata/pending", { method: "DELETE" });
  } catch (e) {
    /* discarding is best-effort */
  }
  hide(els.metaReview);
  applyConfig(await (await fetch("/api/config")).json());
  loadMetadata();
}

// --------------------------------------------------------------------------- //
// Persistent sidebar metadata editor
//
// Always available once a file is loaded. Shares the prose-editor component and
// the preview/save helpers with the Excel review modal.
// --------------------------------------------------------------------------- //

/** Fetch the RAW metadata document and (re)render the sidebar editor. */
async function loadMetadata() {
  if (!state.ready) { hide(els.metaEditor); return; }

  let doc;
  try {
    doc = await (await fetch("/api/metadata")).json();
  } catch (e) {
    hide(els.metaEditor);
    return;
  }
  if (doc.error) { hide(els.metaEditor); return; }

  renderProseEditor(els.metaProse, doc);

  // A short line on the file's state: whether a sidecar exists and, if its
  // prose was edited since the blocks were generated, that a Save is due.
  let hint = doc.exists ? "Editing the sidecar next to this file." : "No metadata file yet — add notes and Save to create one.";
  if (doc.stale) hint += " Prose changed since last generate; Save to refresh.";
  els.metaHint.textContent = hint;

  renderReconSummary(els.metaRecon, doc.reconcile);
  show(els.metaEditor);
}

async function saveMetadataSidebar() {
  const data = await saveProse(els.metaProse, els.metaRecon, els.metaSaveBtn);
  if (!data) return;
  // A saved sidecar changes the schema view (descriptions, codes) too.
  await loadSchema();
  applyConfig(await (await fetch("/api/config")).json());
}

// --------------------------------------------------------------------------- //
// A "turn": one question and everything it produced (SQL, picker, results).
// Each turn owns its own DOM subtree and state, so past turns stay on screen.
// --------------------------------------------------------------------------- //

function createTurn(question) {
  const node = els.turnTemplate.content.firstElementChild.cloneNode(true);
  const turn = {
    root: node,
    sqlSection: node.querySelector(".sql-section"),
    explanation: node.querySelector(".explanation"),
    confidence: node.querySelector(".confidence"),
    sqlBox: node.querySelector(".sql-box"),
    runBtn: node.querySelector(".run-btn"),
    pickerSection: node.querySelector(".picker-section"),
    pickerList: node.querySelector(".picker-list"),
    applyBtn: node.querySelector(".apply-btn"),
    status: node.querySelector(".status"),
    resultsSection: node.querySelector(".results-section"),
    resultsMeta: node.querySelector(".results-meta"),
    resultsTable: node.querySelector(".results-table"),
    copyBtn: node.querySelector(".copy-btn"),
    csvBtn: node.querySelector(".csv-btn"),
    mismatches: [],
    _result: null,
  };

  node.querySelector(".q-text").textContent = question;
  turn.runBtn.addEventListener("click", () => runSql(turn, []));
  turn.applyBtn.addEventListener("click", () => applyPicks(turn));
  turn.copyBtn.addEventListener("click", () => copyResult(turn));
  turn.csvBtn.addEventListener("click", () => downloadCsv(turn, node.querySelector(".q-text").textContent));

  els.history.appendChild(node);
  node.scrollIntoView({ behavior: "smooth", block: "start" });
  return turn;
}

function setStatus(turn, message, isError = false, spinner = false) {
  if (!message) { hide(turn.status); return; }
  turn.status.className = "status" + (isError ? " error" : "");
  turn.status.innerHTML = (spinner ? '<span class="spinner"></span>' : "") + escapeHtml(message);
  show(turn.status);
}

function showSql(turn, sql, explanation, confidence) {
  turn.sqlBox.value = sql || "";
  turn.explanation.textContent = explanation || "";
  if (confidence) {
    turn.confidence.textContent = confidence;
    turn.confidence.className = "confidence " + confidence;
  } else {
    turn.confidence.textContent = "";
  }
  show(turn.sqlSection);
}

// --------------------------------------------------------------------------- //
// Ask a natural-language question
// --------------------------------------------------------------------------- //

async function askQuery() {
  const query = els.nlInput.value.trim();
  if (!query) return;
  els.nlInput.value = "";

  const turn = createTurn(query);
  els.askBtn.disabled = true;
  setStatus(turn, "Thinking… (waiting for the model)", false, true);

  try {
    const data = await postJSON("/api/query", { query });
    if (data.error && !data.sql) {
      setStatus(turn, data.error, true);
      return;
    }

    showSql(turn, data.sql, data.explanation, data.confidence);
    if (data.error) {
      setStatus(turn, data.error, true);
      return;
    }

    if (data.mismatches && data.mismatches.length) {
      setStatus(turn, "");
      showPicker(turn, data.mismatches);
    } else if (data.result) {
      renderResult(turn, data.result);
    }
  } catch (e) {
    setStatus(turn, "Request failed: " + e.message, true);
  } finally {
    els.askBtn.disabled = false;
  }
}

// --------------------------------------------------------------------------- //
// Value picker
// --------------------------------------------------------------------------- //

function showPicker(turn, mismatches) {
  turn.mismatches = mismatches;
  turn.pickerList.innerHTML = "";

  mismatches.forEach((mismatch, index) => {
    const row = document.createElement("div");
    row.className = "picker-row";

    const label = document.createElement("label");
    label.innerHTML =
      `<span class="col">${escapeHtml(mismatch.column)}</span> = ` +
      `<span class="bad">'${escapeHtml(mismatch.attempted_value)}'</span> — pick a real value:`;

    const select = document.createElement("select");
    select.dataset.index = index;
    const keep = document.createElement("option");
    keep.value = "";
    keep.textContent = "— keep as typed —";
    select.appendChild(keep);
    for (const candidate of mismatch.candidates) {
      const option = document.createElement("option");
      option.value = candidate;
      option.textContent = candidate;
      select.appendChild(option);
    }

    row.appendChild(label);
    row.appendChild(select);
    turn.pickerList.appendChild(row);
  });

  show(turn.pickerSection);
}

async function applyPicks(turn) {
  const selects = turn.pickerList.querySelectorAll("select");
  const choices = [];
  selects.forEach((select) => {
    const mismatch = turn.mismatches[Number(select.dataset.index)];
    if (select.value) {
      choices.push({
        column: mismatch.column,
        attempted_value: mismatch.attempted_value,
        chosen: select.value,
      });
    }
  });
  await runSql(turn, choices);
  hide(turn.pickerSection);
}

// --------------------------------------------------------------------------- //
// Run SQL (from a turn's editor, or after applying value picks)
// --------------------------------------------------------------------------- //

async function runSql(turn, choices = []) {
  const sql = turn.sqlBox.value.trim();
  if (!sql) return;

  turn.runBtn.disabled = true;
  hide(turn.resultsSection);
  setStatus(turn, "Running query…", false, true);

  try {
    const data = await postJSON("/api/run-sql", { sql, choices });
    if (data.error) {
      setStatus(turn, data.error, true);
      return;
    }
    if (data.sql) turn.sqlBox.value = data.sql; // reflect applied value picks
    renderResult(turn, data);
  } catch (e) {
    setStatus(turn, "Request failed: " + e.message, true);
  } finally {
    turn.runBtn.disabled = false;
  }
}

function renderResult(turn, result) {
  if (result.error) {
    setStatus(turn, result.error, true);
    return;
  }
  setStatus(turn, "");

  const { columns, rows, row_count } = result;
  turn._result = result;
  turn.resultsMeta.textContent = `${row_count} row${row_count === 1 ? "" : "s"}`;

  const thead = "<thead><tr>" +
    columns.map((c) => `<th>${escapeHtml(c)}</th>`).join("") +
    "</tr></thead>";

  const tbody = "<tbody>" +
    rows.map((row) =>
      "<tr>" +
      row.map((value) =>
        value === null
          ? '<td class="null">NULL</td>'
          : `<td>${escapeHtml(value)}</td>`
      ).join("") +
      "</tr>"
    ).join("") +
    "</tbody>";

  turn.resultsTable.innerHTML = thead + tbody;
  show(turn.resultsSection);
}

// --------------------------------------------------------------------------- //
// Export helpers
// --------------------------------------------------------------------------- //

function resultToTsv(result) {
  const escape = (v) => (v === null ? "" : String(v).replace(/\t/g, " ").replace(/\n/g, " "));
  const rows = [result.columns, ...result.rows];
  return rows.map((row) => row.map(escape).join("\t")).join("\n");
}

function resultToCsv(result) {
  const quote = (v) => {
    const s = v === null ? "" : String(v);
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const rows = [result.columns, ...result.rows];
  return rows.map((row) => row.map(quote).join(",")).join("\n");
}

async function copyResult(turn) {
  if (!turn._result) return;
  const tsv = resultToTsv(turn._result);
  try {
    await navigator.clipboard.writeText(tsv);
    const orig = turn.copyBtn.textContent;
    turn.copyBtn.textContent = "Copied!";
    setTimeout(() => { turn.copyBtn.textContent = orig; }, 1500);
  } catch {
    // fallback: select a temporary textarea
    const ta = document.createElement("textarea");
    ta.value = tsv;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
    const orig = turn.copyBtn.textContent;
    turn.copyBtn.textContent = "Copied!";
    setTimeout(() => { turn.copyBtn.textContent = orig; }, 1500);
  }
}

function downloadCsv(turn, question) {
  if (!turn._result) return;
  const csv = resultToCsv(turn._result);
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const slug = question.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 48) || "results";
  a.href = url;
  a.download = `${slug}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// --------------------------------------------------------------------------- //
// Wire up
// --------------------------------------------------------------------------- //

els.askForm.addEventListener("submit", (e) => {
  e.preventDefault();
  askQuery();
});

els.loadBtn.addEventListener("click", () => loadSession(true));
els.fileSelect.addEventListener("change", () => loadSession());
els.modelSelect.addEventListener("change", () => loadSession());

els.uploadBtn.addEventListener("click", () => els.fileInput.click());
els.fileInput.addEventListener("change", () => {
  const file = els.fileInput.files && els.fileInput.files[0];
  if (file) uploadFile(file);
});

els.mrPreview.addEventListener("click", () =>
  previewProse(els.mrEditor, els.mrRecon, els.mrPreview),
);
els.mrSave.addEventListener("click", saveMetadataReview);
els.mrDiscard.addEventListener("click", discardMetadataReview);

els.metaToggle.addEventListener("click", () => {
  const collapsed = els.metaBody.classList.toggle("hidden");
  els.metaToggle.querySelector(".caret").textContent = collapsed ? "▸" : "▾";
});
els.metaPreviewBtn.addEventListener("click", () =>
  previewProse(els.metaProse, els.metaRecon, els.metaPreviewBtn),
);
els.metaSaveBtn.addEventListener("click", saveMetadataSidebar);

setReady(false);
loadConfig();
