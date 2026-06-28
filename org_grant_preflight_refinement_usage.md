# Organization-only grant preflight refinement

This patch changes the preflight grant warning logic so `GrantsToIndividualsInd`
does not create a missing-detail warning by itself.

Run from your repo folder:

```powershell
cd C:\projects\irs990-tool
python apply_org_grant_preflight_refinement.py rebuild_irs990_slim_clean.py
```

Then rerun the same 5,000-file preflight. For the 990PF file you spot-checked,
where:

```xml
<GrantsToIndividualsInd>true</GrantsToIndividualsInd>
<GrantsToOrganizationsInd>false</GrantsToOrganizationsInd>
```

the preflight should no longer warn merely because individual grants were reported.

The warning is still kept when:
- `GrantsToOrganizationsInd` is true;
- `MoreThan5000KToOrgInd` is true;
- grant amount fields are positive and the filing is not explicitly individual-only.
