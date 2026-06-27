# Proposed documentation changes

This bundle proposes a central documentation structure for `SaveOregonSchools/irs990-tool`.

## Add

- `README.md` — new central project README.
- `queries/README.md` — query module/plugin guide.
- `ai/README.md` — Ask Database schema guide notes.
- `config/README.md` — Ollama complexity config notes.

## Replace/update

- `db/README.md` — expanded from a placeholder into a useful local DB guide.
- `README_rebuild_irs990_slim_clean.md` — updated to use the repo filename `rebuild_irs990_slim_clean.py` instead of the older/local `*_v3.py` name.
- `GRANT_AI_ASSIST_README.md` — cleaned up and updated to use the repo filename `resolve_grant_recipients.py` instead of the older/local `resolve_grant_recipients_v2_1_fast.py` name.

## Recommendation

Keep multiple README files, but give each one a clear scope:

- Root `README.md`: orientation, setup, workflow, and links.
- Subfolder READMEs: local instructions for that folder.
- Component READMEs: deep operational guides for rebuild and grant-recipient matching.

That is cleaner than one huge README and avoids losing the detailed operational notes.
