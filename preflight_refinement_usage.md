# Preflight refinement patch

Run this after the earlier `apply_preflight_to_rebuild.py` patch has already been applied.

```powershell
cd C:\projects\irs990-tool
python apply_preflight_refinements.py rebuild_irs990_slim_clean.py
```

Then rerun your sample preflight:

```powershell
python rebuild_irs990_slim_clean.py `
  --xml-dir C:\IRSDB\XML\17-18 `
  --preflight `
  --workers 1 `
  --preflight-max-files 5000 `
  --preflight-report exports\preflight_sample_summary_v2.json `
  --preflight-csv exports\preflight_sample_files_v2.csv
```

Expected changes:
- The ElementTree `DeprecationWarning` messages should disappear.
- `unknown_form_version_combo` should drop substantially or disappear for the 2014v6.0 / 2015v3.0 / 2016v3.0 combos observed in your 5,000-file sample.
