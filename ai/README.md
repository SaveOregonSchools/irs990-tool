# AI schema guide

This folder contains the compact schema guide used by the Ask Database query module:

```text
ai/irs990_ai_schema.md
```

The guide is included in the prompt sent to Ollama when the user asks a plain-English database question.

---

## Purpose

`irs990_ai_schema.md` tells the model:

- which SQLite tables/views are approved
- which columns are safe to use
- how common joins should be written
- how to distinguish filers/grantors from grant recipients/grantees
- how to calculate common totals, such as grant amount
- that generated SQL must be read-only and include a numeric `LIMIT`

The goal is to keep the model focused on the actual slim schema and reduce hallucinated table or column names.

---

## When to update it

Update `irs990_ai_schema.md` when:

- a table or view is added to the approved Ask Database validator
- a column is added, renamed, or removed from an approved table/view
- a common query pattern is consistently being generated incorrectly
- a new module introduces a normalized view that should be available to natural-language questions

Keep it compact. The model performs better when the guide is focused on the tables and patterns it is actually allowed to use.

---

## Relationship to `queries/ask_database.py`

`queries/ask_database.py` loads this file and combines it with the user's question. The module then:

1. calls Ollama,
2. cleans the generated SQL,
3. validates that the query is read-only,
4. checks that approved tables/views are used,
5. checks known qualified column references,
6. requires a numeric `LIMIT`, and
7. optionally runs a preview.

If the model references a new table/view in this schema guide but `ask_database.py` does not approve it, validation will still block the query. Keep the schema guide and validator aligned.
