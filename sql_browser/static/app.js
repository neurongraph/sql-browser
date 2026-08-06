"use strict";

const $ = (id) => document.getElementById(id);

const els = {
  dbName: $("db-name"),
  schemaTree: $("schema-tree"),
  askForm: $("ask-form"),
  nlInput: $("nl-input"),
  askBtn: $("ask-btn"),
  suggestions: $("suggestions"),
  history: $("history"),
  turnTemplate: $("turn-template"),
};

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
// Schema sidebar
// --------------------------------------------------------------------------- //

async function loadSchema() {
  try {
    const data = await (await fetch("/api/schema")).json();
    els.dbName.textContent = data.database || "";
    renderSchema(data.tables || []);
  } catch (e) {
    els.schemaTree.textContent = "Failed to load schema.";
  }
}

function renderSchema(tables) {
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
        hint;
      cols.appendChild(li);
    }

    name.addEventListener("click", () => {
      cols.style.display = cols.style.display === "none" ? "block" : "none";
    });

    wrap.appendChild(name);
    wrap.appendChild(cols);
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

loadSchema();
loadSuggestions();
