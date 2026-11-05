-- AI failures: what AWS said when it gave up on a visit (06-failed.json, or one line of a batch 06-failed.jsonl).
-- INTERNAL ONLY: never logged, never sent to the Backend or the wrapper, never deleted (except with its visit).
-- Safe to run on a database that already has it.
BEGIN;

CREATE TABLE IF NOT EXISTS ai_failures (
  visit_id     bigint NOT NULL REFERENCES visits (visit_id) ON DELETE CASCADE,
  analysis     analysis_kind NOT NULL DEFAULT 'visit',
  source       text NOT NULL,                              -- urgent, on-demand, symptom-checker or batch
  lane         text NOT NULL CHECK (lane IN ('normal','critical')),
  batch_id     text,                                       -- the batch of a line of 06-failed.jsonl
  failed_step  text,                                       -- the AWS step, only when it looks like a name
  error        text,                                       -- AWS's short message (cut to 500)
  failed_at    timestamptz,                                -- when AWS gave up (best effort)
  aws_status   text,                                       -- AWS's own word, normally 'failed' (cut to 40)
  record_id    text,                                       -- the recordId in the line (cut to 100)
  details      text,                                       -- AWS's text (cut to 8192); the raw line when it was unreadable
  received_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (visit_id, analysis)
);

COMMIT;
