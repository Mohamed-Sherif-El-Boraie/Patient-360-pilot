-- Patient-360 pilot: initial schema (PostgreSQL 16+). One transaction, no circular foreign keys.
-- One visit = one row. The symptom check is a second ANALYSIS of the same visit: it moves the stage on
-- (received_symptom ...) and its data lives in rows with analysis = 'symptom'.
BEGIN;

-- The progress of a visit, one list. The *_symptom stages are the symptom check of the same visit.
CREATE DOMAIN progress_stage AS text CHECK (VALUE IN (
  'received', 'validated', 'processed', 'queued', 'sent_to_aws', 'output_received', 'post_processing',
  'delivered', 'failed',
  'received_symptom', 'validated_symptom', 'processed_symptom', 'queued_symptom', 'sent_to_aws_symptom',
  'output_received_symptom', 'post_processing_symptom', 'delivered_symptom', 'failed_symptom'));

-- Which run of a visit a row belongs to.
CREATE DOMAIN analysis_kind AS text CHECK (VALUE IN ('visit', 'symptom'));

-- ===== small lookup tables: the Backend sends the number, we keep the number and the name ===========
CREATE TABLE sources (
  source_id   int PRIMARY KEY,
  source_name text NOT NULL UNIQUE
);
INSERT INTO sources (source_id, source_name) VALUES
  (1, 'urgent'), (2, 'batch'), (3, 'symptom-checker'), (4, 'on-demand');

CREATE TABLE companies (
  company_id   int PRIMARY KEY,
  company_name text NOT NULL UNIQUE
);
INSERT INTO companies (company_id, company_name) VALUES (15, 'El-Borg'), (10, 'Al-Mokhtbar');

CREATE TABLE sex (
  sex_id   int PRIMARY KEY,
  sex_name text NOT NULL UNIQUE
);
INSERT INTO sex (sex_id, sex_name) VALUES (1, 'Male'), (2, 'Female'), (3, 'Any');   -- 3 is for packages only

-- ===== reference data: tests, parameters and packages ==============================================
CREATE TABLE tests (
  test_id              int PRIMARY KEY,
  test_name_en         text NOT NULL,
  test_name_ar         text,
  test_description_en  text,
  test_description_ar  text,
  test_precautions_en  text,
  test_precautions_ar  text,
  category             text                    -- groups tests; matches parameters.category (retrieval rule)
);
CREATE INDEX tests_category ON tests (category);

CREATE TABLE parameters (                                -- a result parameter (one test can have several)
  parameter_id   int PRIMARY KEY,
  parameter_name text NOT NULL,
  category       text                          -- same list of categories as tests.category
);

CREATE TABLE packages (
  package_id              int PRIMARY KEY,
  package_name_en         text NOT NULL,               -- may repeat for other ages / genders
  package_name_ar         text,
  package_type            text NOT NULL CHECK (package_type IN ('WELLNESS','GENERAL')),
  package_description_en  text,
  package_description_ar  text,
  category                text,                         -- ROUTINE / NON-ROUTINE (no CHECK: a new one needs no migration)
  sex_id                  int NOT NULL DEFAULT 3 REFERENCES sex (sex_id),   -- 1 Male, 2 Female, 3 Any
  age_min                 int CHECK (age_min >= 0),     -- years, NULL = no lower limit
  age_max                 int,                          -- years, NULL = no upper limit
  active                  boolean NOT NULL DEFAULT true,
  CHECK (age_min IS NULL OR age_max IS NULL OR age_min <= age_max)
);

CREATE TABLE test_package_map (                          -- which tests are inside which package; both must exist
  package_id int NOT NULL REFERENCES packages (package_id),
  test_id    int NOT NULL REFERENCES tests (test_id),
  PRIMARY KEY (package_id, test_id)
);
CREATE INDEX test_package_map_test ON test_package_map (test_id);

-- ===== the batch and the visit ======================================================================
CREATE SEQUENCE batch_normal_seq;                        -- normal_000001, normal_000002 ...
CREATE SEQUENCE batch_critical_seq;                      -- critical_000001 ...

CREATE TABLE batches (                                   -- one batch of 100 visits sent to AWS (the lane is in the id)
  batch_id    text PRIMARY KEY CHECK (batch_id ~ '^(normal|critical)_[0-9]{6,}$'),   -- our own id, known before AWS
  aws_job_id  text UNIQUE,                               -- filled after submission
  status      text NOT NULL DEFAULT 'created'
              CHECK (status IN ('created','submitted','completed','partially_completed','failed')),
  -- partially_completed: AWS finished but some visits failed; those are retried until every visit is good
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE visits (                                    -- ONE row per visit; the JSON is in record_payloads
  visit_id            bigint PRIMARY KEY,
  patient_id          bigint NOT NULL,
  idempotency_key     text NOT NULL UNIQUE,              -- HMAC(patient|visit): our own key (blob names, internal dedupe)
  source_id           int NOT NULL REFERENCES sources (source_id) CHECK (source_id IN (1, 2, 4)),   -- the visit's own source
  company_id          int REFERENCES companies (company_id),
  age_days            int,                                 -- age at the visit, in days
  sex_id              int REFERENCES sex (sex_id) CHECK (sex_id IN (1, 2)),   -- a patient is Male or Female
  tier                text CHECK (tier IN ('NORMAL','MILD','MODERATE','CRITICAL')),
  lane                text GENERATED ALWAYS AS (            -- computed from the tier, so the two never disagree
                        CASE WHEN tier IN ('NORMAL','MILD') THEN 'normal'
                             WHEN tier IN ('MODERATE','CRITICAL') THEN 'critical' END) STORED,
  batch_id            text REFERENCES batches (batch_id),
  stage               progress_stage NOT NULL DEFAULT 'received',
  patient_symptoms    text CHECK (char_length(patient_symptoms) <= 500),   -- set when the symptom check arrives
  created_at          timestamptz NOT NULL DEFAULT now(),  -- when we received the visit
  symptom_received_at timestamptz,                         -- when we received the symptom check
  used_symptom_checker boolean GENERATED ALWAYS AS (symptom_received_at IS NOT NULL) STORED,   -- did the patient use it?
  updated_at          timestamptz NOT NULL DEFAULT now()
) WITH (fillfactor = 70, autovacuum_vacuum_scale_factor = 0.02);
CREATE INDEX visits_unbatched ON visits (lane, created_at) WHERE batch_id IS NULL AND source_id = 2;
CREATE INDEX visits_batch ON visits (batch_id) WHERE batch_id IS NOT NULL;
CREATE INDEX visits_patient ON visits (patient_id);

CREATE VIEW visits_readable AS                           -- the visit with the names behind the numbers
  SELECT v.*, s.source_name, c.company_name, x.sex_name
  FROM visits v
  JOIN sources s USING (source_id)
  LEFT JOIN companies c USING (company_id)
  LEFT JOIN sex x USING (sex_id);

-- ===== the JSON itself, insert-only and versioned ==================================================
CREATE TABLE record_payloads (
  visit_id   bigint NOT NULL REFERENCES visits (visit_id) ON DELETE CASCADE,
  analysis   analysis_kind NOT NULL DEFAULT 'visit',
  kind       text NOT NULL CHECK (kind IN ('raw','processed','output_internal','output_public')),
  version    int  NOT NULL DEFAULT 1,                    -- +1 when the same kind is produced again (re-run), history is kept
  body       jsonb NOT NULL CHECK (jsonb_typeof(body) = 'object'),   -- searchable, NOT byte-exact
  raw_bytes  bytea,                                      -- exactly what the Backend sent (kind raw only)
  sha256     text NOT NULL,                              -- fingerprint of the stored JSON, to compare with the Blob copy
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (visit_id, analysis, kind, version),
  CHECK ((kind = 'raw') = (raw_bytes IS NOT NULL))
);
CREATE VIEW record_payloads_current AS
  SELECT DISTINCT ON (visit_id, analysis, kind) *
  FROM record_payloads ORDER BY visit_id, analysis, kind, version DESC;

-- bytea is shown as hex by psql; this turns it back into the text that was sent
CREATE FUNCTION bytes_to_text(raw bytea) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
  RETURN convert_from(raw, 'UTF8');
EXCEPTION WHEN character_not_in_repertoire OR untranslatable_character THEN
  RETURN encode(raw, 'escape');                          -- not valid UTF-8: show the bytes as escaped text
END $$;

CREATE TABLE rejected_payloads (                         -- failed validation: may lack ids, so never in visits
  rejected_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id   int,                                       -- whatever the payload claimed, may be missing
  raw_bytes   bytea NOT NULL,
  errors      jsonb NOT NULL,
  blob_path   text,
  received_at timestamptz NOT NULL DEFAULT now()
);
CREATE VIEW rejected_payloads_readable AS                -- the same rows with the exact text that arrived
  SELECT rejected_id, source_id, bytes_to_text(raw_bytes) AS raw_text, errors, blob_path, received_at
  FROM rejected_payloads;

-- ===== what the Backend sent, as structured rows (text, as received) ================================
CREATE TABLE visit_tests (                               -- the tests of a visit (a test has several parameters)
  visit_id    bigint NOT NULL REFERENCES visits (visit_id) ON DELETE CASCADE,
  test_id     int    NOT NULL REFERENCES tests (test_id),
  test_remark text,                                      -- the doctor's remark on the test
  PRIMARY KEY (visit_id, test_id)
);

CREATE TABLE lab_results (                               -- one row per parameter of a test in a visit
  visit_id            bigint NOT NULL,
  test_id             int    NOT NULL,
  parameter_id        int    NOT NULL REFERENCES parameters (parameter_id),
  result_date         text,
  value               text,
  unit                text,
  reference_low       text,
  reference_high      text,
  reference_range     text,
  lab_flag            text CHECK (lab_flag IN ('High','Low')),      -- mapped by us from H / L
  critical_low        text,
  critical_high       text,
  critical_range_text text,
  critical_direction  text CHECK (critical_direction IN ('critical_low','critical_high')),   -- added by us
  status              text CHECK (status IN ('high','low','normal','abnormal','critical','text')),   -- added by us
  alert               text CHECK (alert IN ('NORMAL','MEDICAL_ATTENTION','CRITICAL')),      -- added by us; NULL = no alert
  parameter_remark    text,
  PRIMARY KEY (visit_id, test_id, parameter_id),
  FOREIGN KEY (visit_id, test_id) REFERENCES visit_tests (visit_id, test_id) ON DELETE CASCADE
);

CREATE TABLE lab_result_history (                        -- earlier values of the parameter, from earlier visits
  visit_id         bigint NOT NULL,
  test_id          int    NOT NULL,
  parameter_id     int    NOT NULL,
  history_visit_id bigint NOT NULL,                      -- the earlier visit (it may not be in our database)
  history_date     text,
  history_value    text,
  PRIMARY KEY (visit_id, test_id, parameter_id, history_visit_id),
  FOREIGN KEY (visit_id, test_id, parameter_id) REFERENCES lab_results (visit_id, test_id, parameter_id) ON DELETE CASCADE
);

CREATE TABLE descriptive_results (                       -- tests that are text, not numbers
  visit_id    bigint NOT NULL REFERENCES visits (visit_id) ON DELETE CASCADE,
  test_id     int    NOT NULL REFERENCES tests (test_id),
  result_date text,
  text_value  text NOT NULL,
  status      text NOT NULL DEFAULT 'text' CHECK (status IN ('high','low','normal','abnormal','critical','text')),   -- added by us
  PRIMARY KEY (visit_id, test_id)
);

CREATE TABLE descriptive_result_history (
  visit_id         bigint NOT NULL,
  test_id          int    NOT NULL,
  history_visit_id bigint NOT NULL,
  history_date     text,
  history_value    text,
  PRIMARY KEY (visit_id, test_id, history_visit_id),
  FOREIGN KEY (visit_id, test_id) REFERENCES descriptive_results (visit_id, test_id) ON DELETE CASCADE
);

CREATE TABLE visit_answers (                             -- the patient's answers to the preparation questions
  visit_id         bigint NOT NULL REFERENCES visits (visit_id) ON DELETE CASCADE,
  preparation_id   int    NOT NULL,
  preparation_name text   NOT NULL,
  answer           text,
  PRIMARY KEY (visit_id, preparation_id)
);

-- ===== what Bedrock answered, as structured rows (analysis = 'visit' or 'symptom') ==================
CREATE TABLE ai_outputs (                                -- one row per visit and analysis
  visit_id             bigint NOT NULL REFERENCES visits (visit_id) ON DELETE CASCADE,
  analysis             analysis_kind NOT NULL DEFAULT 'visit',
  status               text NOT NULL CHECK (status IN ('success','guardrail_blocked','error','answered')),
  summary_en           text, summary_ar text,
  simple_summary_en    text, simple_summary_ar text,
  note_en              text, note_ar text,
  html_en              text,                           -- visit results only; NULL until the HTML format exists
  html_ar              text,
  generated_at         timestamptz,
  PRIMARY KEY (visit_id, analysis)
);

CREATE TABLE ai_model_calls (                            -- INTERNAL ONLY: each model call behind a result
  visit_id                    bigint NOT NULL,
  analysis                    analysis_kind NOT NULL DEFAULT 'visit',
  purpose                     text NOT NULL CHECK (purpose IN ('analysis','translation')),
  model_id                    text NOT NULL,
  mode                        text CHECK (mode IN ('on_demand','batch_inference')),
  input_tokens                int,
  output_tokens               int,
  cache_creation_input_tokens int,
  cache_read_input_tokens     int,
  stop_reason                 text,                    -- end_turn, max_tokens, stop_sequence, tool_use, refusal ...
  stop_sequence               text,                    -- NULL unless a stop sequence ended the answer
  PRIMARY KEY (visit_id, analysis, purpose),
  FOREIGN KEY (visit_id, analysis) REFERENCES ai_outputs (visit_id, analysis) ON DELETE CASCADE
);

CREATE TABLE ai_critical_alerts (                        -- the list of critical alerts, one row each
  ai_critical_alert_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  visit_id             bigint NOT NULL,
  analysis             analysis_kind NOT NULL DEFAULT 'visit',
  alert_en             text NOT NULL,
  alert_ar             text,
  FOREIGN KEY (visit_id, analysis) REFERENCES ai_outputs (visit_id, analysis) ON DELETE CASCADE
);
CREATE INDEX ai_critical_alerts_visit ON ai_critical_alerts (visit_id, analysis);

CREATE TABLE ai_recommendations (                        -- recommended tests; the advisory is per test
  visit_id            bigint NOT NULL,
  analysis            analysis_kind NOT NULL DEFAULT 'visit',
  test_id             int  NOT NULL REFERENCES tests (test_id),
  priority            text NOT NULL CHECK (priority IN ('urgent','important')),
  priority_ar         text,
  change_type         text CHECK (change_type IN ('kept','added','removed')),   -- symptom check only; NULL for a visit
  change_type_ar      text,
  advisory_level      text CHECK (advisory_level IN ('none','discuss_with_doctor','seek_care_promptly')),
  advisory_level_ar   text,
  advisory_message_en text, advisory_message_ar text,
  reason_en text, reason_ar text,
  explanation_en text, explanation_ar text,              -- why kept, added or removed (symptom check)
  timing_en text, timing_ar text,
  guideline_en text, guideline_ar text,
  PRIMARY KEY (visit_id, analysis, test_id),
  FOREIGN KEY (visit_id, analysis) REFERENCES ai_outputs (visit_id, analysis) ON DELETE CASCADE
);

CREATE TABLE record_packages (                           -- packages chosen for a visit (the rule lives in the application)
  visit_id   bigint NOT NULL REFERENCES visits (visit_id) ON DELETE CASCADE,
  package_id int    NOT NULL REFERENCES packages (package_id),
  rank       int    NOT NULL CHECK (rank >= 1),
  score      numeric(6,4),                              -- only if the owner's rule produces one
  PRIMARY KEY (visit_id, package_id)
);
CREATE TABLE record_package_tests (
  visit_id   bigint NOT NULL,
  package_id int    NOT NULL,
  test_id    int    NOT NULL REFERENCES tests (test_id),
  PRIMARY KEY (visit_id, package_id, test_id),
  FOREIGN KEY (visit_id, package_id) REFERENCES record_packages (visit_id, package_id) ON DELETE CASCADE
);

-- ===== Bedrock guardrails: input (symptom check) and output (every source) ==========================
CREATE TABLE guardrail_checks (                          -- one row per guardrail check
  guardrail_check_id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  visit_id                          bigint NOT NULL REFERENCES visits (visit_id) ON DELETE CASCADE,
  analysis                          analysis_kind NOT NULL DEFAULT 'visit',
  direction                         text NOT NULL CHECK (direction IN ('input','output')),
  guardrail_id                      text NOT NULL,       -- the key in the Bedrock trace, e.g. gr7x2k9m4p1q
  action                            text NOT NULL CHECK (action IN ('NONE','INTERVENED')),
  latency_ms                        int,
  text_chars_guarded                int,
  text_chars_total                  int,
  topic_policy_units                int,
  content_policy_units              int,
  word_policy_units                 int,
  sensitive_info_policy_units       int,
  sensitive_info_policy_free_units  int,
  contextual_grounding_policy_units int,
  created_at                        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (visit_id, analysis, direction, guardrail_id)
);

CREATE TABLE guardrail_findings (                        -- what the guardrail found: topics and content filters
  guardrail_finding_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  guardrail_check_id   bigint NOT NULL REFERENCES guardrail_checks (guardrail_check_id) ON DELETE CASCADE,
  policy               text NOT NULL CHECK (policy IN ('topic','content')),
  name                 text,                             -- topic name (topic policy only)
  type                 text NOT NULL,                    -- topic type, or content filter type
  confidence           text,                             -- content filters only
  filter_strength      text,                             -- content filters only
  action               text NOT NULL
);
CREATE INDEX guardrail_findings_check ON guardrail_findings (guardrail_check_id);

-- ===== work tracking: who is processing which run, and retries ======================================
CREATE TABLE record_processing (                         -- exactly ONE current row per visit and analysis
  visit_id        bigint NOT NULL REFERENCES visits (visit_id) ON DELETE CASCADE,
  analysis        analysis_kind NOT NULL DEFAULT 'visit',
  read_from       text NOT NULL CHECK (read_from IN ('blob','postgresql')),
  status          text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','processing','sent','delivered','failed')),
  claimed_by      text,                                 -- e.g. 'normal-batch', 'urgent'
  attempts        int  NOT NULL DEFAULT 0,
  next_retry_at   timestamptz,
  last_error      text,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (visit_id, analysis)
) WITH (fillfactor = 70, autovacuum_vacuum_scale_factor = 0.02);
CREATE INDEX record_processing_pending ON record_processing (updated_at) WHERE status = 'pending';

CREATE TABLE record_processing_attempts (                -- insert-only history of every attempt
  record_processing_attempt_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  visit_id         bigint NOT NULL REFERENCES visits (visit_id) ON DELETE CASCADE,
  analysis         analysis_kind NOT NULL DEFAULT 'visit',
  attempt          int  NOT NULL,
  started_at       timestamptz NOT NULL DEFAULT now(),
  finished_at      timestamptz,
  outcome          text CHECK (outcome IN ('sent','retry','failed')),
  error            text
);
CREATE INDEX record_processing_attempts_visit ON record_processing_attempts (visit_id, analysis);

-- ===== timing: how long each phase took ============================================================
CREATE TABLE record_phase_timings (
  visit_id    bigint NOT NULL REFERENCES visits (visit_id) ON DELETE CASCADE,
  analysis    analysis_kind NOT NULL DEFAULT 'visit',
  phase       text NOT NULL CHECK (phase IN
              ('validate','process','queue_wait','aws_round_trip','post_process','deliver')),
  attempt     int  NOT NULL DEFAULT 1,
  started_at  timestamptz NOT NULL,
  duration_ms int  NOT NULL CHECK (duration_ms >= 0),
  PRIMARY KEY (visit_id, analysis, phase, attempt)
);

-- the tracing view: visit | which run | where it is read from | status | claimed by
CREATE VIEW processing_trace AS
  SELECT visit_id, analysis, read_from, status, claimed_by FROM record_processing;

-- one row per visit and analysis: when it arrived and how many seconds each phase took (attempts added up)
CREATE VIEW record_timeline AS
  SELECT v.visit_id, a.analysis, v.source_id,
         CASE a.analysis WHEN 'visit' THEN v.created_at ELSE v.symptom_received_at END AS received_at,
         sum(t.duration_ms) FILTER (WHERE t.phase = 'validate')       / 1000.0 AS validate_seconds,
         sum(t.duration_ms) FILTER (WHERE t.phase = 'process')        / 1000.0 AS process_seconds,
         sum(t.duration_ms) FILTER (WHERE t.phase = 'queue_wait')     / 1000.0 AS queue_wait_seconds,
         sum(t.duration_ms) FILTER (WHERE t.phase = 'aws_round_trip') / 1000.0 AS aws_round_trip_seconds,
         sum(t.duration_ms) FILTER (WHERE t.phase = 'post_process')   / 1000.0 AS post_process_seconds,
         sum(t.duration_ms) FILTER (WHERE t.phase = 'deliver')        / 1000.0 AS deliver_seconds,
         sum(t.duration_ms) / 1000.0                                           AS total_seconds
  FROM visits v
  CROSS JOIN (VALUES ('visit'::analysis_kind), ('symptom'::analysis_kind)) AS a(analysis)
  LEFT JOIN record_phase_timings t ON t.visit_id = v.visit_id AND t.analysis = a.analysis
  WHERE a.analysis = 'visit' OR v.symptom_received_at IS NOT NULL
  GROUP BY v.visit_id, a.analysis;

COMMIT;
