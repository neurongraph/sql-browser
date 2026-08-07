# Backlog

Enhancements deferred from the metadata layer (Phase 1), plus gaps found while
building and verifying it. Roughly ordered by value per unit of work within each
priority band.

Phase 1 delivered: declared foreign keys (including composite keys, grouped so a
single-column join on them is never presented as valid), curated YAML sidecar
(descriptions, value dictionaries, synonyms, grain, join paths, glossary,
conventions, rules), additive-only reconciliation, label→code resolution in the
query corrector, mid-session metadata reload, and `--init-metadata` scaffolding.

Phase 2 delivered: relevance-based prompt pruning (`render_schema_context` and
`get_schema_description` now take an optional `question`; tables not scored
relevant are rendered as a one-line summary instead of full column detail, with
one-hop join-graph expansion so a bridge table is never dropped, and a fallback
to "include everything" whenever scoring can't confidently narrow anything
down), and curated few-shot examples (`examples:` in the metadata file,
schema-validated at reconciliation like everything else, selected per question
by the same lexical scoring and rendered as real prior chat turns rather than
prompt prose). Both the CLI and web UI compute the pruned schema and matching
examples fresh per question; see `context.relevant_tables`, `context.
select_examples`, `SQLBrowserSession._query_context`, and `web.py`'s
`run_query`.

Phase 3 delivered (prose-canonical metadata): a single authoring pipeline —
*free-form prose → per-section LLM parse → reconcile against the live schema →
structured YAML* — fed by two front doors. Prose is now the canonical artifact,
embedded as a `prose:` block in the sidecar; the structured blocks are a
generated, reconciled artifact stamped with a `_generated.from_prose_digest` so
staleness is detected at load without ever calling the model. Delivered pieces:
Excel documentation-sheet detection and split (`excel_loader.plan_sheets`,
`convert_with_metadata_detection`, `convert_xlsx_to_sqlite(skip_sheets=…)`) so a
workbook whose metadata sheet has blank rows no longer fails the whole load;
comment-preserving round-trip writes via `ruamel.yaml` (`metadata.write_document`,
`load_prose`, `prose_digest`, `regenerate_structured`); all new prompt text in
`context.py` (`build_prose_parse_prompt`, `build_metadata_sheet_classifier_prompt`,
`schema_hint_for_*`); `llm.parse_prose_to_metadata` / `classify_sheet`; a
web editor (persistent sidebar editor plus an Excel-review modal that offers to
convert an in-workbook documentation sheet) backed by `GET/POST /api/metadata`,
`POST /api/metadata/preview`, and `DELETE /api/metadata/pending`; and CLI parity
via `--regenerate-metadata`. Reconciliation stays the single gate, so the
additive-never-authoritative guarantee is unchanged and legacy structured-only
sidecars load identically.

---

## P1 — do these next

Empty for now - both items that were here (relevance-based prompt pruning and
few-shot examples) shipped in Phase 2, above. The next tier of value is P2.

---

## P2 — clear value, more design needed

### 1. Named metrics

```yaml
metrics:
  revenue:
    sql: SUM(order_items.qty * order_items.unit_price_cents) / 100.0
    requires: [order_items]
    synonyms: [sales, turnover, top line]
```

The `glossary` already covers this in prose, and prose worked in verification
(`gemma4:31b-cloud` correctly derived the cancelled-order filter from the
glossary entry alone). So the marginal value here is *consistency* — an exact
expression the model copies rather than re-derives differently each time.

Open question worth resolving before building: whether metrics are prompt hints
or something the guard can substitute deterministically. The latter is much more
valuable and much harder — it needs the metric's `requires` tables to be present
and correctly joined in the generated query before substitution is safe.

### 2. Metadata-aware join validation in `sql_guard.py`

The relationship graph is currently advisory — it's in the prompt, but nothing
checks the generated SQL against it. `sql_guard` could detect a join predicate
between two columns with no declared relationship and feed a repair hint back
through the existing loop:

> `orders` and `products` aren't directly related; join via `order_items`.

Must be a hint, not a hard rejection, and must degrade like everything else in
that module. Watch for false positives: legitimate cross joins, CTEs, self-joins,
and correlated subqueries all need to pass. Given the graph is often incomplete
(nobody documents every edge), leaning toward hints over blocks is not just
safer, it's correct.

### 3. Standalone metadata validation

Right now you have to start a session to find out your metadata file has drifted.
A `--check-metadata` mode flag (following `--init-metadata`) that prints the
reconciliation warnings and exits non-zero would make it usable in CI, so a
schema migration that invalidates documentation fails a build rather than
silently degrading prompts months later.

Cheap: `resolve_metadata()` already returns exactly the warning list needed, and
Phase 3 added the sibling mode flags `--init-metadata` / `--regenerate-metadata`
to copy the shape from — this is now a small addition.

### 4. Mitigating wrong-but-valid value codes

Found during verification and worth recording precisely. Asked "how many orders
are in transit?", `granite4.1:8b` generated `status = 'processing'`. The metadata
file documents `'shipped'` with the synonym `in transit`, and that synonym was in
the prompt. The corrector can't catch this: `'processing'` is a real value, so
there's no mismatch to detect, and a valid filter is indistinguishable from an
intended one.

`gemma4:31b-cloud` gets it right, so this is model capacity rather than a design
flaw — but it's the most likely residual failure mode for small models, and the
metadata to catch it already exists.

Note the *metadata-caused* version of this is already handled: a label or synonym
claimed by two codes is reported at load time and `resolve_label()` declines to
rewrite it, so the corrector can no longer turn an ambiguous phrase into a
wrong-but-valid code on its own. What remains is the model choosing one unaided.

Possible approaches, none obviously right:

- A post-generation check: for each filtered column that has a value dictionary,
  test whether any *word in the user's question* matches a documented label or
  synonym for a **different** code than the one used, and if so surface it as a
  confirmable suggestion (reusing the existing value-picker UI in both
  interfaces).
- Render dictionaries more forcefully in the prompt (e.g. a compact `user says →
  filter on` mapping table rather than an inline parenthetical).

The first is deterministic and reuses machinery that already exists. Worth a
spike to see how often it fires falsely on a real database.

---

## P3 — worthwhile, not urgent

### 5. Additional metadata sources

The loader is deliberately source-agnostic, so these are drop-ins:

- **Embedded `_sql_browser_metadata` table**, mirroring the profiler's sidecar
  cache pattern. Travels with the `.db` file; harder to hand-edit.
- ~~**`__metadata__` sheet in an Excel workbook**~~ — ✅ **Delivered (Phase 3).**
  A workbook's documentation sheet is detected (name heuristic, with an optional
  LLM classifier fallback), split off from the data sheets, and its text
  extracted as prose the review modal offers to convert into a sidecar. The
  extracted prose still writes to the `<stem>.meta.yaml` sidecar rather than back
  into the workbook, so it survives a copy only if the sidecar travels with it —
  writing metadata *into* the `.xlsx` remains open.

Excel is where this matters most — a workbook declares no foreign keys, so
`relationships:` is the only way its sheets can be connected. With the sheet now
ingested as prose, documenting those joins in the workbook and having them
reconciled into the sidecar is the natural next step.

### 6. `pii` → result masking

`pii: true` currently only annotates the prompt. It could also mask values in
displayed results unless explicitly asked for. Needs a decision on where masking
belongs (`query_executor.py` for the CLI, the `/api/query` payload for the web
UI) and an obvious escape hatch — a flag or a per-query override — since the
whole point of a browser is sometimes to look at the data.

### 7. Web UI metadata editing — ✅ Delivered (Phase 3)

Shipped as the prose-canonical editor: a collapsible sidebar editor bound to the
RAW document (prose + pre-reconcile structured), Preview (dry-run reconcile) and
Save, plus the Excel-review modal. The write endpoint, `ruamel.yaml`
round-tripping and staleness detection all landed. Still open: on-disk conflict
handling if the sidecar is edited by hand *while* the web session holds it — Save
currently rewrites from the session's view without checking for a newer file.

### 8. Test coverage for the web wiring

`metadata.py`, `context.py`, `excel_loader.py` and the CLI's metadata wiring
(including `--regenerate-metadata`) are covered. `tests/test_web.py` now covers
the `/api/query` pruning/example wiring (Phase 2) and the Phase 3 metadata
endpoints: the raw-vs-reconciled binding (`GET /api/metadata` vs `/api/schema`),
preview-writes-nothing, Save-writes-and-reloads, graceful degradation when Ollama
is down, and Excel workbook load offering metadata while loading only the data
sheet — which exercises `load_file()` for the Excel path. Still uncovered by a
test: the frontend itself (the modal-visibility CSS regression that shipped in A7
and was fixed by hand is exactly the kind of thing no Python test would catch).

### 9. `default_time_column` is prompt-only

It tells the model which column "last month" means, and nothing enforces it. It
could inform a deterministic check on relative date filters, in the same spirit
as the value corrector.

---

## Rejected / not planned

- **Metadata overriding physical facts.** Considered and deliberately rejected:
  metadata is additive only, so a stale file can never make the tool assert
  something false about the data. Every consumer depends on that guarantee.
  Anything needing an override is really asking for a fix to the data or a view.
- ~~**A YAML round-tripping dependency for scaffolding.**~~ Revisited and adopted
  in Phase 3, exactly as flagged: editing (not just generation) needed it, so
  `ruamel.yaml` now backs `metadata.write_document` for comment-preserving
  round-trip writes. The hand-rolled writer (`build_scaffold`) is still used for
  `--init-metadata`, where there is nothing to preserve.
