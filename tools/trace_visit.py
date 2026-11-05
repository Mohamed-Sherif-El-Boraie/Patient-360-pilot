"""Where is a visit? Reads PostgreSQL only (same PG* settings as the app), prints one block per table.

    python /app/tools/trace_visit.py             # the 5 latest visits, one line each
    python /app/tools/trace_visit.py 1795782760780   # one visit in full: stage, processing, attempts, answer, payloads
    python /app/tools/trace_visit.py rejected    # the 3 latest 422 payloads: errors and the start of what arrived
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
        visit_id = int(sys.argv[1])
        for title, sql in ONE.items():
            show(title, conn.execute(sql, (visit_id,) * sql.count("%s")).fetchall())


if __name__ == "__main__":
    main()
