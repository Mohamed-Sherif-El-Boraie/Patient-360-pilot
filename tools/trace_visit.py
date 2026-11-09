"""Where is a visit? Reads PostgreSQL only (same PG* settings as the app), prints one block per table.

    python /app/tools/trace_visit.py             # the 5 latest visits, one line each
    python /app/tools/trace_visit.py 1795782760780   # one visit in full: stage, processing, attempts, answer, payloads
    python /app/tools/trace_visit.py rejected    # the 3 latest 422 payloads: errors and the start of what arrived
    python /app/tools/trace_visit.py batches     # batch visits waiting, by stage, and the latest batches
    python /app/tools/trace_visit.py batch normal_000002   # one batch (no id: the latest): AWS answers, failures
    python /app/tools/trace_visit.py stuck       # what is waiting: deliveries, failed uploads, no AWS answer, _pending
"""
import sys

import psycopg
from psycopg.rows import dict_row

from patient360.config.settings import load_settings

LATEST = ("SELECT visit_id, source_id, tier, lane, stage, symptom_flag, created_at, updated_at "
          "FROM visits ORDER BY created_at DESC LIMIT 5")
ONE = {
    "visits": "SELECT visit_id, patient_id, company_id, source_id, tier, lane, stage, symptom_flag, "
              "used_symptom_checker, created_at, updated_at FROM visits WHERE visit_id = %s",
    "record_processing (the queue: upload to S3, then deliver)":
        "SELECT analysis, step, status, attempts, claimed_by, next_retry_at, last_error, updated_at "
        "FROM record_processing WHERE visit_id = %s ORDER BY analysis",
    "record_processing_attempts":
        "SELECT analysis, attempt, started_at, finished_at, outcome, error "
        "FROM record_processing_attempts WHERE visit_id = %s ORDER BY record_processing_attempt_id",
    "record_phase_timings (where the time went)":
        "SELECT analysis, phase, attempt, started_at, duration_ms FROM record_phase_timings "
        "WHERE visit_id = %s ORDER BY analysis, started_at",
    "ai_outputs": "SELECT analysis, status, aws_review_status, generated_at FROM ai_outputs WHERE visit_id = %s",
    "ai_failures (what AWS said when it gave up; details cut to 200)":
        "SELECT analysis, source, lane, batch_id, failed_step, error, failed_at, aws_status, record_id, "
        "left(details, 200) AS details_preview, received_at FROM ai_failures WHERE visit_id = %s",
    "ai_model_calls": "SELECT analysis, purpose, model_id, mode, input_tokens, output_tokens, "
                      "cache_creation_input_tokens, cache_read_input_tokens, stop_reason "
                      "FROM ai_model_calls WHERE visit_id = %s ORDER BY analysis, purpose",
    "record_payloads (kinds saved)": "SELECT analysis, kind, version, length(raw_bytes) AS bytes, created_at "
                                     "FROM record_payloads WHERE visit_id = %s ORDER BY created_at",
    "recommendations / alerts":
        "SELECT (SELECT count(*) FROM ai_recommendations WHERE visit_id = %s) AS recommendations, "
        "(SELECT count(*) FROM ai_critical_alerts WHERE visit_id = %s) AS critical_alerts",
}
BATCH = {
    "batch {}": "SELECT batch_id, aws_job_id, status, read_from, attempts, last_error, created_at FROM batches "
                "WHERE batch_id = %s",
    "visits of the batch, by stage": "SELECT stage, count(*) AS visits FROM visits WHERE batch_id = %s "
                                     "GROUP BY stage ORDER BY stage",
    "their queue (record_processing)":
        "SELECT rp.step, rp.status, count(*) AS visits, max(rp.attempts) AS max_attempts, "
        "max(rp.last_error) AS last_error FROM record_processing rp JOIN visits v USING (visit_id) "
        "WHERE v.batch_id = %s AND rp.analysis = 'visit' GROUP BY rp.step, rp.status ORDER BY rp.step, rp.status",
    "answers from AWS (ai_outputs)":
        "SELECT o.status, o.aws_review_status, count(*) AS visits, max(o.generated_at) AS last_answer "
        "FROM ai_outputs o JOIN visits v USING (visit_id) WHERE v.batch_id = %s AND o.analysis = 'visit' "
        "GROUP BY o.status, o.aws_review_status",
    "failures from AWS (ai_failures)":
        "SELECT failed_step, left(error, 120) AS error, count(*) AS visits FROM ai_failures WHERE batch_id = %s "
        "GROUP BY failed_step, left(error, 120)",
}


STUCK = {
    "results waiting to be delivered to the wrapper (it keeps failing: retried every 15 minutes, no limit)":
        "SELECT v.visit_id, rp.analysis, rp.attempts, rp.next_retry_at, left(rp.last_error, 120) AS last_error "
        "FROM record_processing rp JOIN visits v USING (visit_id) WHERE rp.step = 'deliver' AND rp.status = 'pending' "
        "AND rp.attempts > 0 ORDER BY rp.attempts DESC, v.visit_id LIMIT 20",
    "results the wrapper refused for good (not retried)":
        "SELECT v.visit_id, rp.analysis, rp.attempts, left(rp.last_error, 160) AS last_error, rp.updated_at "
        "FROM record_processing rp JOIN visits v USING (visit_id) WHERE rp.step = 'deliver' AND rp.status = 'failed' "
        "ORDER BY rp.updated_at DESC LIMIT 20",
    "uploads to S3 that gave up (the Backend was told success false)":
        "SELECT v.visit_id, rp.analysis, rp.attempts, rp.last_error, rp.updated_at FROM record_processing rp "
        "JOIN visits v USING (visit_id) WHERE rp.step = 'upload' AND rp.status = 'failed' "
        "ORDER BY rp.updated_at DESC LIMIT 20",
    "sent to AWS more than 1 hour ago and no answer yet (by source)":
        "SELECT s.source_name, count(*) AS visits, min(v.updated_at) AS oldest FROM visits v "
        "JOIN sources s USING (source_id) WHERE v.stage IN ('sent_to_aws', 'sent_to_aws_symptom') "
        "AND v.updated_at < now() - interval '1 hour' GROUP BY s.source_name ORDER BY oldest",
    "visits saved but never processed (the 5-minute sweep should take them)":
        "SELECT v.visit_id, v.source_id, v.created_at FROM visits v WHERE v.created_at < now() - interval '15 minutes' "
        "AND NOT EXISTS (SELECT 1 FROM record_processing p WHERE p.visit_id = v.visit_id AND p.analysis = 'visit') "
        "ORDER BY v.created_at LIMIT 20",
}


def parked(settings) -> None:
    """The payloads kept in Blob (``_pending``) because a step of ours failed while receiving them."""
    from patient360.domain.common.blob_paths import PENDING_PREFIX
    from patient360.runtime import raw_store
    print("\n== parked payloads waiting in p360-raw/_pending (saved by the recovery when the database works)")
    try:
        names = raw_store(settings).list(PENDING_PREFIX)
    except Exception as error:
        print(f"  could not list them ({type(error).__name__}): check with az storage blob list --prefix _pending")
        return
    print(f"  ({len(names)})")
    for name in names[:20]:
        print("  " + name)


def show(title, rows):
    print(f"\n== {title}  ({len(rows)})")
    for row in rows:
        print("  " + "  ".join(f"{k}={v}" for k, v in row.items()))


def main() -> None:
    s = load_settings()
    with psycopg.connect(host=s.pg_host, port=s.pg_port, dbname=s.pg_database, user=s.pg_user,
                         password=s.pg_password, sslmode=s.pg_ssl_mode, connect_timeout=10, row_factory=dict_row,
                         options=f"-c search_path={s.pg_schema}" if s.pg_schema else None) as conn:
        if len(sys.argv) < 2:
            show("latest visits", conn.execute(LATEST).fetchall())
            return
        if sys.argv[1] == "rejected":      # what the Backend really sent when we answered 422
            show("latest rejected payloads (raw_text is the exact text that arrived)", conn.execute(
                "SELECT rejected_id, received_at, source_id, blob_path, errors, length(raw_text) AS chars, "
                "left(raw_text, 700) AS raw_start FROM rejected_payloads_readable "
                "ORDER BY rejected_id DESC LIMIT 3").fetchall())
            return
        if sys.argv[1] == "batches":       # did the 120 batch visits make a batch?
            show("batch visits waiting for a batch (a batch is made when a lane has BATCH_SIZE of them)",
                 conn.execute("SELECT v.lane, v.tier, count(*) AS visits FROM visits v "
                              "JOIN record_processing rp ON rp.visit_id = v.visit_id AND rp.analysis = 'visit' "
                              "AND rp.step = 'upload' AND rp.status = 'pending' "
                              "WHERE v.source_id = 2 AND v.batch_id IS NULL GROUP BY v.lane, v.tier "
                              "ORDER BY v.lane, v.tier").fetchall())
            show("batch visits by stage", conn.execute(
                "SELECT lane, stage, count(*) AS visits, count(batch_id) AS in_a_batch FROM visits "
                "WHERE source_id = 2 GROUP BY lane, stage ORDER BY lane, stage").fetchall())
            show("latest batches", conn.execute(
                "SELECT b.batch_id, b.status, b.read_from, b.attempts, b.last_error, b.created_at, "
                "(SELECT count(*) FROM visits v WHERE v.batch_id = b.batch_id) AS visits FROM batches b "
                "ORDER BY b.created_at DESC LIMIT 5").fetchall())
            return
        if sys.argv[1] == "stuck":         # everything that is waiting on us or on someone else, in one look
            for title, sql in STUCK.items():
                show(title, conn.execute(sql).fetchall())
            parked(s)
            return
        if sys.argv[1] == "batch":         # one batch: has AWS answered, and did the answers reach the Backend?
            batch_id = sys.argv[2] if len(sys.argv) > 2 else conn.execute(
                "SELECT batch_id FROM batches ORDER BY created_at DESC LIMIT 1").fetchone()["batch_id"]
            for title, sql in BATCH.items():
                show(title.format(batch_id), conn.execute(sql, (batch_id,) * sql.count("%s")).fetchall())
            return
        visit_id = int(sys.argv[1])
        for title, sql in ONE.items():
            show(title, conn.execute(sql, (visit_id,) * sql.count("%s")).fetchall())


if __name__ == "__main__":
    main()
