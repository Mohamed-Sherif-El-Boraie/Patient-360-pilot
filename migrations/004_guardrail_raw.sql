-- Guardrails: what AWS reports besides the 001 columns. INTERNAL ONLY.
--   guardrail_checks.bypassed   the batch guardrail object says if the guardrail was bypassed
--   guardrail_checks.raw        the assessment(s) exactly as AWS gave them (jsonb)
--   guardrail_findings.raw      the entry exactly as AWS gave it (jsonb)
--   guardrail_findings.policy   also word, sensitive_info and contextual_grounding (001 allowed topic and content only)
-- Safe to run on a database that already has it.
BEGIN;

ALTER TABLE guardrail_checks
  ADD COLUMN IF NOT EXISTS bypassed boolean,
  ADD COLUMN IF NOT EXISTS raw      jsonb;

ALTER TABLE guardrail_findings
  ADD COLUMN IF NOT EXISTS raw jsonb;

ALTER TABLE guardrail_findings DROP CONSTRAINT IF EXISTS guardrail_findings_policy_check;
ALTER TABLE guardrail_findings ADD CONSTRAINT guardrail_findings_policy_check
  CHECK (policy IN ('topic','content','word','sensitive_info','contextual_grounding'));

COMMIT;
