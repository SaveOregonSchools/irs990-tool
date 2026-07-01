REM Windows batch script to run all commands to support enhanced grant matching
REM Run only after loading new XML files into database
@ECHO OFF

ECHO This batch file is used to build/rebuild the data supporting the enhanced grant matching feature
ECHO This only needs to be run after loading new XML files into the database
ECHO .
ECHO The entire process can take many hours, depending on the amount of IRS data you have
ECHO .

:start
SET choice=
SET /p choice=Do you want to continue? [Y/N]:
IF NOT '%choice%'=='' SET choice=%choice:~0,1%
IF '%choice%'=='Y' GOTO yes
IF '%choice%'=='y' GOTO yes
IF '%choice%'=='N' GOTO no
IF '%choice%'=='n' GOTO no
IF '%choice%'=='' GOTO no
ECHO "%choice%" is not valid
ECHO .
GOTO start

:yes
REM Go to root of IRS 990 Tool project folder
cd C:\projects\irs990-tool

REM Refresh deterministic grant resolution
python resolve_grant_recipients.py `
  --db C:\projects\irs990-tool\db\irs990.db `
  --full-refresh `
  --batch-size 100000

REM Rebuild org identity
python grant_ai_assist_v1.py build-identity --full-refresh

REM Rebuild signatures
python grant_ai_assist_v1.py build-signatures --full-refresh

REM Regenerate candidates
python grant_ai_assist_v1.py generate-candidates --full-refresh --candidate-mode fast
python grant_ai_assist_v1.py generate-candidates --candidate-mode balanced --queue-status no_candidates

REM Reported EIN triage
python grant_ai_assist_v1.py reported-ein-triage

REM Nonadjudicable/list-style recipients
python grant_ai_assist_v1.py nonadjudicable-recipient-triage --action human_review

REM Park blank-recipient-name signatures so they do not go to AI
python grant_ai_assist_v1.py nonadjudicable-recipient-triage --action human_review --include-blank-recipient-name

REM High-confidence candidate rules
python grant_ai_assist_v1.py candidate-rule-decisions --rules exact_name_zip,exact_name_city_state,exact_address_zip_good_name

REM Single high-score candidate rule
python grant_ai_assist_v1.py candidate-rule-decisions --rules single_candidate_high_score

REM Exact name + exact address + state rule
python grant_ai_assist_v1.py candidate-rule-decisions --rules exact_name_state_only

REM Large safe remaining alias
python grant_ai_assist_v1.py candidate-rule-decisions --rules large_safe_remaining

REM Address/name remaining rule
python grant_ai_assist_v1.py candidate-rule-decisions --rules address_name_remaining

REM Looser 0.70 address/name remaining rule
python grant_ai_assist_v1.py candidate-rule-decisions --rules address_name_remaining --addr-name-min-name-score 0.70 --high-address-geo-min-name-score 0.70

REM Distinctive exact-name / no-geo rule
python grant_ai_assist_v1.py candidate-rule-decisions --rules exact_name_no_geo_distinctive

REM Rebuild the final layer / apply all decisions
python grant_ai_assist_v1.py apply-decisions --full-refresh

REM Run a recount to see remaining signatures, grants, and total dollar amount that require AI adjudication
sqlite3 C:\projects\irs990-tool\db\irs990.db "SELECT COUNT(*) AS signatures_left_for_ai_review, COALESCE(SUM(s.grant_count),0) AS grants_represented, ROUND(COALESCE(SUM(s.total_amount),0),2) AS total_amount FROM grant_recipient_signature s WHERE EXISTS (SELECT 1 FROM grant_recipient_ai_candidate c WHERE c.signature_hash = s.signature_hash) AND NOT EXISTS (SELECT 1 FROM grant_recipient_ai_decision d WHERE d.signature_hash = s.signature_hash);"

pause

:no