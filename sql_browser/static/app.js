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
  } else {
    els.schemaTree.textContent = "No file loaded.";
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
    mismatches: [],
  };

  node.querySelector(".q-text").textContent = question;
  turn.runBtn.addEventListener("click", () => runSql(turn, []));
  turn.applyBtn.addEventListener("click", () => applyPicks(turn));

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

setReady(false);
loadConfig();
