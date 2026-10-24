"""Pins the schema decisions (no database needed). Run the migration on a real PostgreSQL for the real check."""
import re
from pathlib import Path

from patient360.domain.timing import PHASES

SQL = re.sub(r"[ \t]+", " ", (Path(__file__).parent.parent / "migrations" / "001_init.sql").read_text(encoding="utf-8"))

TABLES = [
    "sources", "companies", "sex", "tests", "parameters", "packages", "test_package_map", "batches",
    "visits", "record_payloads", "rejected_payloads", "visit_tests", "lab_results", "lab_result_history",
    "descriptive_results", "descriptive_result_history", "visit_answers", "ai_outputs", "ai_model_calls",
    "ai_critical_alerts", "ai_recommendations", "record_packages", "record_package_tests", "guardrail_checks",
    "guardrail_findings", "record_processing", "record_processing_attempts", "record_phase_timings",
]
VIEWS = ["visits_readable", "record_payloads_current", "rejected_payloads_readable", "processing_trace",
         "record_timeline"]


def table(name: str) -> str:
    match = re.search(rf"CREATE TABLE {name} \((.*?)\n\)", SQL, re.DOTALL)
    assert match, f"CREATE TABLE {name} is missing"
    return match.group(1)


def test_every_expected_table_and_view_is_declared():
    for name in TABLES:
        table(name)
    for name in VIEWS:
        assert f"CREATE VIEW {name} AS" in SQL
    assert len(re.findall(r"CREATE TABLE ", SQL)) == len(TABLES)


def test_one_row_per_visit_and_one_stage_column_with_the_symptom_stages():
    assert "CREATE TABLE records" not in SQL and "record_id" not in SQL and "catalogue_tests" not in SQL
    visits = table("visits")
    assert "visit_id bigint PRIMARY KEY" in visits and "stage progress_stage" in visits
    for stage in ("post_processing", "received_symptom", "processed_symptom", "queued_symptom", "delivered_symptom"):
        assert f"'{stage}'" in SQL


def test_runs_of_a_visit_are_told_apart_by_the_analysis_column():
    assert "CREATE DOMAIN analysis_kind AS text CHECK (VALUE IN ('visit', 'symptom'))" in SQL
    for name in ("record_payloads", "ai_outputs", "guardrail_checks", "record_processing", "record_phase_timings"):
        assert "analysis analysis_kind" in table(name)
    assert "PRIMARY KEY (visit_id, analysis)" in table("ai_outputs")


def test_the_backend_numbers_have_lookup_tables_with_the_agreed_values():
    assert "(1, 'urgent'), (2, 'batch'), (3, 'symptom-checker'), (4, 'on-demand')" in SQL
    assert "(15, 'El-Borg'), (10, 'Al-Mokhtbar')" in SQL
    assert "(1, 'Male'), (2, 'Female'), (3, 'Any')" in SQL
    visits = table("visits")
    for reference in (
        "REFERENCES sources (source_id)", "REFERENCES companies (company_id)", "REFERENCES sex (sex_id)",
    ):
        assert reference in visits
    assert "source_id IN (1, 2, 4)" in visits          # a visit's own source is never the symptom check
    assert "sex_id IN (1, 2)" in visits                # a patient is Male or Female, never Any
    assert "company_name" not in visits and "sex text" not in visits


def test_the_lane_is_computed_from_the_tier_and_batches_have_no_lane_column():
    assert "lane text GENERATED ALWAYS AS" in table("visits")
    assert "lane text" not in table("batches")
    assert "CHECK (batch_id ~ '^(normal|critical)_[0-9]{6,}$')" in table("batches")
    assert "CREATE SEQUENCE batch_normal_seq" in SQL and "CREATE SEQUENCE batch_critical_seq" in SQL
    assert "aws_job_id text UNIQUE" in table("batches")


def test_there_is_one_idempotency_key_per_visit_stored_on_the_visit():
    assert "idempotency_key text NOT NULL UNIQUE" in table("visits")
    assert SQL.count("idempotency_key text") == 1 and "source_hash" not in SQL


def test_real_foreign_keys_and_no_circular_reference():
    assert "REFERENCES batches (batch_id)" in table("visits")
    assert "last_record_id" not in SQL and "text[]" not in SQL


def test_json_lives_in_the_payload_table_not_on_the_hot_rows():
    assert "jsonb" not in table("visits") and "bytea" not in table("visits")
    payloads = table("record_payloads")
    assert "jsonb_typeof(body) = 'object'" in payloads and "raw_bytes bytea" in payloads


def test_rejected_payloads_do_not_require_ids_and_can_be_read_as_text():
    rejected = table("rejected_payloads")
    assert "visit_id" not in rejected and "patient_id" not in rejected
    assert "CREATE FUNCTION bytes_to_text(raw bytea) RETURNS text" in SQL
    assert "bytes_to_text(raw_bytes) AS raw_text" in SQL


def test_hot_tables_are_tuned_for_frequent_updates():
    for name in ("visits", "record_processing"):
        assert re.search(rf"CREATE TABLE {name} \(.*?\) WITH \(fillfactor = 70", SQL, re.DOTALL)


def test_results_are_identified_by_visit_test_and_parameter_with_real_foreign_keys():
    lab = table("lab_results")
    assert "PRIMARY KEY (visit_id, test_id, parameter_id)" in lab
    assert "REFERENCES parameters (parameter_id)" in lab
    assert "FOREIGN KEY (visit_id, test_id) REFERENCES visit_tests (visit_id, test_id)" in lab
    visit_tests = table("visit_tests")
    assert "REFERENCES tests (test_id)" in visit_tests and "test_remark text" in visit_tests
    assert "CHECK (status IN ('high','low','normal','abnormal','critical','text'))" in lab
    assert "CHECK (alert IN ('NORMAL','MEDICAL_ATTENTION','CRITICAL'))" in lab and "NOT_CLASSIFIED" not in SQL
    for removed in ("lab_result_id", "previous_value", "previous_date", "test_name", "parameter_name", "value_num"):
        assert removed not in lab
    for column in ("result_date text", "value text", "reference_low text", "critical_low text",
                   "critical_range_text text", "parameter_remark text"):
        assert column in lab


def test_history_is_per_parameter_and_per_earlier_visit():
    history = table("lab_result_history")
    assert "history_visit_id bigint NOT NULL" in history
    assert "history_date text" in history and "history_value text" in history
    assert "FOREIGN KEY (visit_id, test_id, parameter_id) REFERENCES lab_results" in history
    assert "history_visit_id bigint NOT NULL REFERENCES" not in history      # the earlier visit may not be ours
    descriptive_history = table("descriptive_result_history")
    assert "history_visit_id bigint NOT NULL" in descriptive_history and "history_value text" in descriptive_history


def test_descriptive_results_and_visit_answers_belong_to_the_visit():
    descriptive = table("descriptive_results")
    assert "text_value text NOT NULL" in descriptive and "REFERENCES tests (test_id)" in descriptive
    assert "status text NOT NULL DEFAULT 'text'" in descriptive
    answers = table("visit_answers")
    assert "REFERENCES visits (visit_id)" in answers and "preparation_id int NOT NULL" in answers
    assert "preparation_name text NOT NULL" in answers


def test_packages_tests_and_the_map_between_them():
    packages = table("packages")
    for column in ("package_name_en text", "package_name_ar text", "package_type text", "package_description_en text",
                   "age_min int", "age_max int"):
        assert column in packages
    assert "category" not in packages
    assert "CHECK (package_type IN ('WELLNESS','GENERAL'))" in packages
    assert "sex_id int NOT NULL DEFAULT 3 REFERENCES sex (sex_id)" in packages and "gender text" not in packages
    mapping = table("test_package_map")
    assert "REFERENCES packages (package_id)" in mapping and "REFERENCES tests (test_id)" in mapping
    assert "PRIMARY KEY (package_id, test_id)" in mapping
    assert "test_aliases" not in SQL


def test_the_tests_table_has_names_descriptions_and_precautions_in_both_languages():
    tests = table("tests")
    for column in ("test_name_en", "test_name_ar", "test_description_en", "test_description_ar",
                   "test_precautions_en", "test_precautions_ar"):
        assert column in tests


def test_ai_output_has_arabic_html_and_the_advisory_only_per_recommendation():
    outputs = table("ai_outputs")
    for column in ("summary_ar text", "html_en text", "html_ar text"):
        assert column in outputs
    assert "html_en text NOT NULL" not in outputs and "advisory" not in outputs and "highest_alert" not in outputs
    recommendations = table("ai_recommendations")
    for column in (
        "test_id int NOT NULL", "priority_ar text", "change_type text", "change_type_ar text", "explanation_en text",
        "advisory_level text", "advisory_level_ar text", "advisory_message_en text",
    ):
        assert column in recommendations
    assert "CHECK (change_type IN ('kept','added','removed'))" in recommendations
    assert "ai_removed_recommendations" not in SQL and "ai_critical_alerts" in SQL


def test_guardrails_are_kept_for_input_and_output_with_their_findings():
    checks = table("guardrail_checks")
    assert "direction text NOT NULL CHECK (direction IN ('input','output'))" in checks
    for column in ("latency_ms", "text_chars_guarded", "topic_policy_units", "contextual_grounding_policy_units"):
        assert column in checks
    assert "CHECK (policy IN ('topic','content'))" in table("guardrail_findings")


def test_every_timed_phase_is_allowed_by_the_timings_table():
    timings = table("record_phase_timings")
    for phase in PHASES:
        assert f"'{phase}'" in timings


def test_the_timeline_view_reports_seconds_per_phase_for_each_analysis():
    view = SQL[SQL.index("CREATE VIEW record_timeline"):]
    for phase in PHASES:
        assert f"{phase}_seconds" in view
    assert "received_at" in view and "total_seconds" in view and "a.analysis" in view


def test_model_usage_is_internal_in_its_own_table_and_there_is_no_cost_column():
    outputs = table("ai_outputs")
    for removed in ("model_id", "input_tokens", "output_tokens", "cost"):
        assert removed not in outputs
    calls = table("ai_model_calls")
    for column in ("purpose text NOT NULL", "model_id text NOT NULL", "input_tokens int", "output_tokens int",
                   "cache_creation_input_tokens int", "cache_read_input_tokens int", "stop_reason text",
                   "stop_sequence text"):
        assert column in calls
    assert "PRIMARY KEY (visit_id, analysis, purpose)" in calls and "cost" not in calls


def test_the_visit_says_whether_the_symptom_checker_was_used_without_changing_its_source():
    visits = table("visits")
    assert "used_symptom_checker boolean GENERATED ALWAYS AS (symptom_received_at IS NOT NULL) STORED" in visits
    assert "source_id IN (1, 2, 4)" in visits          # the symptom call never overwrites the visit's own source


def test_children_of_a_visit_are_deleted_with_it_but_reference_tables_are_not_cascaded():
    parents = (r"visits \(visit_id\)|visit_tests \(visit_id, test_id\)|lab_results \(visit_id, test_id, parameter_id\)"
               r"|descriptive_results \(visit_id, test_id\)|ai_outputs \(visit_id, analysis\)"
               r"|record_packages \(visit_id, package_id\)|guardrail_checks \(guardrail_check_id\)")
    assert re.search(rf"REFERENCES ({parents})", SQL)
    assert not re.search(rf"REFERENCES ({parents})(?! ON DELETE CASCADE)", SQL)
    reference = r"tests|parameters|packages|sources|companies|sex|batches"
    assert not re.search(rf"REFERENCES ({reference}) \([^)]*\) ON DELETE CASCADE", SQL)


def test_tests_and_parameters_share_a_category_for_the_retrieval_rule():
    assert "category text" in table("tests") and "category text" in table("parameters")
    assert "CREATE INDEX tests_category ON tests (category)" in SQL


def test_the_visit_keeps_the_age_in_days_only():
    visits = table("visits")
    assert "age_days" in visits and "age_years" not in visits
