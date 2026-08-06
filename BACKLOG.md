# Backlog

Enhancements deferred from the metadata layer (Phase 1), plus gaps found while
building and verifying it. Roughly ordered by value per unit of work within each
priority band.

Phase 1 delivered: declared foreign keys (including composite keys, grouped so a
single-column join on them is never presented as valid), curated YAML sidecar
(descriptions, value dictionaries, synonyms, grain, join paths, glossary,
conventions, rules), additive-only reconciliation, label→code resolution in the
query corrector, mid-session metadata reload, and `--init-metadata` scaffolding.

---

## P1 — do these next

### 1. Relevance-based prompt pruning

**This is the blocker for real databases.** The renderer currently emits every
table, every column and every value on every request.

Measured on the 4-table sample database:

| | Prompt size |
| --- | --- |
| Schema + profile + foreign keys | 2,263 chars |
| ...plus `examples/sample.meta.yaml` | 5,376 chars (~1,344 tokens) |

Metadata roughly 2.4×'d the prompt. Extrapolating linearly, a 40-table database
lands near 54,000 chars — enough to blow a small model's context, and enough to
bury the relevant tables among forty irrelevant ones even when it fits. Note the
existing `MAX_ENUM_VALUES_IN_SCHEMA = 8` cap in `context.py` already exists
because the unpruned approach doesn't scale.

Proposed shape, in `context.py`:

- **Always included** (cheap, globally useful): database description,
  conventions, rules, glossary, one line per table, the complete join graph.
- **Included on demand**: full column detail, value dictionaries, metrics and
  few-shot examples — only for tables scored relevant to the current question.

Scoring can start lexically: match question tokens against table/column names
and the `synonyms` the metadata file already provides, then expand one hop along
the join graph so a needed bridge table is never dropped. Embeddings later if
lexical proves too blunt (`nomic-embed-text` is already a common local model).

This requires threading the question into rendering. Today `schema_info` is built
once at load time and reused (`cli.py` `start()`, `web.py` `load_file()`), so the
signature becomes `render_schema_context(inspector, question=None)` with the
current behaviour as the `None` case.

Net effect for wide databases is a prompt *reduction* while carrying more useful
signal — this pays for itself, and makes a 60-table database viable.

### 2. Few-shot examples in the metadata file

Curated question→SQL pairs are usually the single largest quality lever for
small local models, and the schema for them is trivial:

```yaml
examples:
  - question: how many orders shipped last month
    sql: |
      SELECT COUNT(*) FROM orders
      WHERE status = 'S'
        AND ordered_at >= date('now','start of month','-1 month')
        AND ordered_at <  date('now','start of month')
```

Work involved:

- `ExampleQuery` model in `metadata.py`.
- Reconciliation should run each example's SQL through
  `sql_guard.find_schema_problems()` and drop any that don't bind against the
  live schema — the additive-only guarantee applied to examples. An example that
  references a dropped column is worse than no example.
- Render as actual prior turns in the message list rather than as prose in the
  system prompt; models follow demonstrated format far better than described
  format. That means `llm.translate_to_sql()` needs the examples, not just the
  schema string.
- Pair with pruning (#1): select examples by relevance, don't inject all of them.

---

## P2 — clear value, more design needed

### 3. Named metrics

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

### 4. Metadata-aware join validation in `sql_guard.py`

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

### 5. Standalone metadata validation

Right now you have to start a session to find out your metadata file has drifted.
A `--check-metadata` mode flag (following `--init-metadata`) that prints the
reconciliation warnings and exits non-zero would make it usable in CI, so a
schema migration that invalidates documentation fails a build rather than
silently degrading prompts months later.

Cheap: `resolve_metadata()` already returns exactly the warning list needed.

### 6. Mitigating wrong-but-valid value codes

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

### 7. Additional metadata sources

The loader is deliberately source-agnostic, so these are drop-ins:

- **Embedded `_sql_browser_metadata` table**, mirroring the profiler's sidecar
  cache pattern. Travels with the `.db` file; harder to hand-edit.
- **`__metadata__` sheet in an Excel workbook**, so metadata lives in the same
  file the analyst already owns and emails around.

Excel is where this matters most — a workbook declares no foreign keys, so
`relationships:` is the only way its sheets can be connected, and keeping that
inside the workbook means it survives being copied.

### 8. `pii` → result masking

`pii: true` currently only annotates the prompt. It could also mask values in
displayed results unless explicitly asked for. Needs a decision on where masking
belongs (`query_executor.py` for the CLI, the `/api/query` payload for the web
UI) and an obvious escape hatch — a flag or a per-query override — since the
whole point of a browser is sometimes to look at the data.

### 9. Web UI metadata editing

The schema panel now shows descriptions and warnings read-only. Editing them in
the browser and writing back to the YAML would close the loop for non-terminal
users. Meaningful work: needs a write endpoint, comment-preserving YAML
round-tripping (a real `ruamel.yaml` dependency — the current hand-rolled writer
only generates, it can't edit), and conflict handling if the file changed on disk.

### 10. Test coverage for the web wiring

`metadata.py`, `context.py` and the CLI's metadata wiring are covered. The web
side — `BrowserSession.load_file()` and the `/api/schema` payload — was verified
by hand but isn't. `fastapi.testclient` works against `create_app()` and would
cover it cheaply; `tests/test_cli.py` shows the pattern for stubbing out Ollama.

### 11. `default_time_column` is prompt-only

It tells the model which column "last month" means, and nothing enforces it. It
could inform a deterministic check on relative date filters, in the same spirit
as the value corrector.

---

## Rejected / not planned

- **Metadata overriding physical facts.** Considered and deliberately rejected:
  metadata is additive only, so a stale file can never make the tool assert
  something false about the data. Every consumer depends on that guarantee.
  Anything needing an override is really asking for a fix to the data or a view.
- **A YAML round-tripping dependency for scaffolding.** The generator emits text
  directly so it can include explanatory comments, which `yaml.safe_dump` strips.
  Only revisit this alongside #10, which genuinely needs editing rather than
  generation.
