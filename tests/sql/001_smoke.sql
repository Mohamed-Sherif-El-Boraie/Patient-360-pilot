-- Smoke test for 001_init.sql on a REAL PostgreSQL. Run it right after the migration; it rolls back and leaves nothing.
--   docker exec -i pg-test psql -U postgres -d patient360 -v ON_ERROR_STOP=1 < tests\sql\001_smoke.sql
BEGIN;

INSERT INTO tests (test_id, test_name_en) VALUES (1, 'Glucose');
INSERT INTO parameters (parameter_id, parameter_name) VALUES (1, 'Glucose');
INSERT INTO visits (visit_id, patient_id, idempotency_key, source_id, company_id, age_days, sex_id, tier)
  VALUES (9, 7, 'key-9', 2, 15, 14610, 1, 'MILD');

DO $$ BEGIN   -- computed columns and the readable view
  IF (SELECT lane FROM visits WHERE visit_id = 9) <> 'normal' THEN RAISE EXCEPTION 'MILD must give lane normal'; END IF;
  IF (SELECT used_symptom_checker FROM visits WHERE visit_id = 9) THEN RAISE EXCEPTION 'flag must start false'; END IF;
  IF (SELECT source_name || company_name || sex_name FROM visits_readable WHERE visit_id = 9) <> 'batchEl-BorgMale'
    THEN RAISE EXCEPTION 'visits_readable shows the wrong names'; END IF;
END $$;

UPDATE visits SET tier = 'CRITICAL', symptom_received_at = now(), stage = 'received_symptom' WHERE visit_id = 9;
DO $$ BEGIN
  IF (SELECT lane FROM visits WHERE visit_id = 9) <> 'critical' THEN RAISE EXCEPTION 'CRITICAL must give lane critical'; END IF;
  IF NOT (SELECT used_symptom_checker FROM visits WHERE visit_id = 9) THEN RAISE EXCEPTION 'flag must turn true'; END IF;
END $$;

-- a visit with everything under it
INSERT INTO visit_tests (visit_id, test_id, test_remark) VALUES (9, 1, 'remark');
INSERT INTO lab_results (visit_id, test_id, parameter_id, value, status, alert) VALUES (9, 1, 1, '95', 'text', NULL);
INSERT INTO lab_result_history (visit_id, test_id, parameter_id, history_visit_id, history_date, history_value)
  VALUES (9, 1, 1, 5, '2026-01-01', '90');
INSERT INTO visit_answers (visit_id, preparation_id, preparation_name, answer) VALUES (9, 995, 'Fasting hours', '10');
INSERT INTO record_payloads (visit_id, analysis, kind, body, raw_bytes, sha256)
  VALUES (9, 'visit', 'raw', '{"a": 1}', convert_to('{"a": 1}', 'UTF8'), 'abc');
INSERT INTO ai_outputs (visit_id, analysis, status) VALUES (9, 'visit', 'success');
INSERT INTO ai_critical_alerts (visit_id, analysis, alert_en) VALUES (9, 'visit', 'alert');
INSERT INTO ai_recommendations (visit_id, analysis, test_id, priority) VALUES (9, 'visit', 1, 'urgent');
INSERT INTO ai_model_calls (visit_id, analysis, purpose, model_id) VALUES (9, 'visit', 'analysis', 'model-x');
INSERT INTO guardrail_checks (visit_id, analysis, direction, guardrail_id, action) VALUES (9, 'visit', 'output', 'gr1', 'NONE');
INSERT INTO guardrail_findings (guardrail_check_id, policy, type, action)
  SELECT guardrail_check_id, 'content', 'HATE', 'NONE' FROM guardrail_checks WHERE visit_id = 9;
INSERT INTO record_processing (visit_id, analysis, read_from) VALUES (9, 'visit', 'blob');
INSERT INTO record_phase_timings (visit_id, analysis, phase, started_at, duration_ms) VALUES (9, 'visit', 'process', now(), 1500);

DO $$ BEGIN   -- the timeline view reports seconds
  IF (SELECT process_seconds FROM record_timeline WHERE visit_id = 9 AND analysis = 'visit') <> 1.5
    THEN RAISE EXCEPTION 'record_timeline must show 1.5 seconds'; END IF;
END $$;

DO $$ BEGIN   -- the exact text behind the bytes
  IF (SELECT raw_text FROM rejected_payloads_readable LIMIT 1) IS NOT NULL THEN NULL; END IF;
  IF bytes_to_text(convert_to('café', 'UTF8')) <> 'café' THEN RAISE EXCEPTION 'bytes_to_text must return the text'; END IF;
  IF bytes_to_text('\xff'::bytea) IS NULL THEN RAISE EXCEPTION 'bytes_to_text must not fail on bad bytes'; END IF;
END $$;

-- bad data must be refused (each block fails the test if the insert is accepted)
DO $$ BEGIN
  INSERT INTO visits (visit_id, patient_id, idempotency_key, source_id) VALUES (10, 7, 'key-10', 3);
  RAISE EXCEPTION 'source 3 (symptom-checker) must not be a visit source';
EXCEPTION WHEN check_violation THEN NULL; END $$;
DO $$ BEGIN
  INSERT INTO visits (visit_id, patient_id, idempotency_key, source_id, sex_id) VALUES (11, 7, 'key-11', 2, 3);
  RAISE EXCEPTION 'a patient cannot be sex Both';
EXCEPTION WHEN check_violation THEN NULL; END $$;
DO $$ BEGIN
  UPDATE visits SET stage = 'nonsense' WHERE visit_id = 9;
  RAISE EXCEPTION 'an unknown stage must be refused';
EXCEPTION WHEN check_violation THEN NULL; END $$;
DO $$ BEGIN
  INSERT INTO batches (batch_id) VALUES ('bad-id');
  RAISE EXCEPTION 'a batch id must look like normal_000001';
EXCEPTION WHEN check_violation THEN NULL; END $$;
DO $$ BEGIN
  INSERT INTO visit_tests (visit_id, test_id) VALUES (9, 999);
  RAISE EXCEPTION 'an unknown test must be refused';
EXCEPTION WHEN foreign_key_violation THEN NULL; END $$;
DO $$ BEGIN
  INSERT INTO parameters (parameter_id, parameter_name) VALUES (2, 'Other');
  INSERT INTO lab_results (visit_id, test_id, parameter_id, status) VALUES (9, 1, 2, 'unjudged');
  RAISE EXCEPTION 'the old status unjudged must be refused';
EXCEPTION WHEN check_violation THEN NULL; END $$;
DO $$ BEGIN
  INSERT INTO parameters (parameter_id, parameter_name) VALUES (3, 'Other2');
  INSERT INTO lab_results (visit_id, test_id, parameter_id, alert) VALUES (9, 1, 3, 'NOT_CLASSIFIED');
  RAISE EXCEPTION 'the alert NOT_CLASSIFIED must be refused';
EXCEPTION WHEN check_violation THEN NULL; END $$;
DO $$ BEGIN
  INSERT INTO lab_results (visit_id, test_id, parameter_id) VALUES (9, 1, 1);
  RAISE EXCEPTION 'the same parameter twice in one test must be refused';
EXCEPTION WHEN unique_violation THEN NULL; END $$;

-- retention: deleting the visit removes everything under it
DELETE FROM visits WHERE visit_id = 9;
DO $$ BEGIN
  IF (SELECT count(*) FROM lab_results) + (SELECT count(*) FROM lab_result_history) + (SELECT count(*) FROM record_payloads)
     + (SELECT count(*) FROM ai_outputs) + (SELECT count(*) FROM ai_recommendations) + (SELECT count(*) FROM guardrail_findings)
     + (SELECT count(*) FROM record_processing) + (SELECT count(*) FROM record_phase_timings) + (SELECT count(*) FROM visit_answers) <> 0
    THEN RAISE EXCEPTION 'deleting a visit must delete its children'; END IF;
END $$;

SELECT 'SMOKE TEST PASSED' AS result;
ROLLBACK;
