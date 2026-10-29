from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from patient360.domain.common.blob_paths import (
    MergedOutput,
    batch_jsonl_path,
    blob_day,
    merged_output_jsonl_path,
    merged_output_path,
    parse_output_path,
    processed_path,
    raw_path,
    rejected_path,
    rejected_unknown_path,
    s3_processed_key,
)

DAY = date(2026, 10, 24)
KEY = "a1b2c3d4e5f60718293a4b5c6d7e8f90"   # 32 characters, as made by make_idempotency_key
FILE = f"{KEY}.json"


def test_the_file_is_named_by_the_full_32_character_key():
    assert len(KEY) == 32
    assert raw_path("urgent", DAY, KEY) == f"raw/urgent/2026/10/24/{FILE}"


def test_raw_files_are_grouped_by_source_and_day():
    assert raw_path("symptom-checker", DAY, KEY) == f"raw/symptom-checker/2026/10/24/{FILE}"
    assert raw_path("batch", DAY, KEY) == f"raw/batch/2026/10/24/{FILE}"


def test_months_and_days_have_two_digits():
    assert raw_path("on-demand", date(2026, 1, 5), KEY) == f"raw/on-demand/2026/01/05/{FILE}"


def test_a_rejected_payload_is_named_by_its_visit_id():
    assert rejected_path("urgent", DAY, 2070000003) == "raw/_rejected/urgent/2026/10/24/2070000003.json"


def test_processed_files_go_to_the_lane_folder():
    assert processed_path("on-demand", "normal", DAY, KEY) == f"processed/on-demand/normal/2026/10/24/{FILE}"
    assert processed_path("urgent", "critical", DAY, KEY) == f"processed/urgent/critical/2026/10/24/{FILE}"
    assert processed_path("symptom-checker", "normal", DAY, KEY) == (
        f"processed/symptom-checker/normal/2026/10/24/{FILE}")


def test_a_batch_visit_is_inside_its_batch_folder_and_the_batch_has_a_jsonl():
    assert processed_path("batch", "normal", DAY, KEY, batch_id="normal_000001") == (
        f"processed/batch/normal/2026/10/24/normal_000001/{FILE}")
    assert batch_jsonl_path("critical", DAY, "critical_000007") == (
        "processed/batch/critical-jsonls/2026/10/24/critical_000007.jsonl")


def test_the_merged_output_lives_in_the_output_container_of_its_source():
    assert merged_output_path("normal", DAY, KEY) == f"normal/2026/10/24/{KEY}/05-output-merged.json"
    assert merged_output_path("critical", DAY, KEY) == f"critical/2026/10/24/{KEY}/05-output-merged.json"
    assert merged_output_jsonl_path("normal", DAY, "normal_000001") == (
        "normal-jsonls/2026/10/24/normal_000001/05-output-merged.jsonl")


def test_the_s3_key_is_inside_the_bucket_of_the_source():
    assert s3_processed_key("urgent", "critical", DAY, KEY) == f"processed/critical/2026/10/24/00-input/{FILE}"
    assert s3_processed_key("symptom-checker", "normal", DAY, KEY) == f"processed/normal/2026/10/24/00-input/{FILE}"
    assert s3_processed_key("on-demand", "normal", DAY, KEY) == f"processed/normal/2026/10/24/00-input/{FILE}"
    assert s3_processed_key("batch", "normal", DAY, KEY, batch_id="normal_000001") == (
        "processed/normal/2026/10/24/normal_000001/00-input/normal_000001.jsonl")


def test_the_date_folder_is_the_utc_day_the_visit_was_received():
    assert blob_day(datetime(2026, 10, 24, 23, 30, tzinfo=UTC)) == date(2026, 10, 24)
    assert blob_day(datetime(2026, 10, 25, 0, 5, tzinfo=UTC)) == date(2026, 10, 25)


def test_a_time_in_another_zone_is_changed_to_utc_first():
    cairo = timezone(timedelta(hours=2))
    assert blob_day(datetime(2026, 10, 25, 1, 0, tzinfo=cairo)) == date(2026, 10, 24)   # 23:00 UTC the day before


def test_a_time_without_a_zone_is_refused():
    with pytest.raises(ValueError):
        blob_day(datetime(2026, 10, 24, 12, 0))


def test_all_files_of_one_visit_share_the_day_it_was_received():
    received = datetime(2026, 10, 24, 23, 59, tzinfo=UTC)
    day = blob_day(received)   # still used at 00:10 the next day, when the result is saved
    assert raw_path("urgent", day, KEY).split("/")[2:5] == ["2026", "10", "24"]
    assert merged_output_path("normal", day, KEY).split("/")[1:4] == ["2026", "10", "24"]


def test_an_unknown_source_or_lane_is_refused():
    with pytest.raises(ValueError):
        raw_path("walk-in", DAY, KEY)
    with pytest.raises(ValueError):
        processed_path("urgent", "high", DAY, KEY)
    with pytest.raises(ValueError):
        batch_jsonl_path("high", DAY, "normal_000001")


def test_a_batch_file_needs_its_batch_id_and_only_a_batch_file_has_one():
    with pytest.raises(ValueError):
        processed_path("batch", "normal", DAY, KEY)
    with pytest.raises(ValueError):
        processed_path("urgent", "normal", DAY, KEY, batch_id="normal_000001")
    with pytest.raises(ValueError):
        s3_processed_key("batch", "normal", DAY, KEY)


@pytest.mark.parametrize("bad", ["../secret", "a/b", "a.b", "", "x" * 101, "key with space", "ключ"])
def test_a_name_that_could_leave_its_folder_is_refused(bad):
    with pytest.raises(ValueError):
        raw_path("urgent", DAY, bad)
    with pytest.raises(ValueError):
        batch_jsonl_path("normal", DAY, bad)


@pytest.mark.parametrize("bad", [0, -1, True, "5", 1.5])
def test_a_visit_id_must_be_a_positive_whole_number(bad):
    with pytest.raises(ValueError):
        rejected_path("urgent", DAY, bad)


def test_a_rejected_payload_without_a_usable_visit_is_named_by_a_hash_of_its_bytes():
    digest = "0123456789abcdef" * 4
    assert rejected_unknown_path(DAY, digest) == "raw/_rejected/unknown/2026/10/24/unknown_0123456789abcdef.json"
    with pytest.raises(ValueError):
        rejected_unknown_path(DAY, "../../etc/passwd")


KEY32 = "a" * 32


def test_an_output_path_is_read_back_into_its_parts():
    visit = parse_output_path("on-demand", f"critical/2026/10/24/{KEY32}/05-output-merged.json")
    assert visit == MergedOutput("visit", "on-demand", "critical", date(2026, 10, 24), key=KEY32)
    jsonl = parse_output_path("batch", "normal-jsonls/2026/10/25/normal_000001/05-output-merged.jsonl")
    assert jsonl == MergedOutput("batch_jsonl", "batch", "normal", date(2026, 10, 25), batch_id="normal_000001")
    copy = parse_output_path("batch", f"critical/2026/10/25/{KEY32}/05-output-merged.json")
    assert copy == MergedOutput("batch_visit", "batch", "critical", date(2026, 10, 25), key=KEY32)


@pytest.mark.parametrize("source", ["urgent", "on-demand", "symptom-checker", "batch"])
def test_a_failed_file_is_a_failure_in_every_source_container_the_batch_one_too(source):
    found = parse_output_path(source, f"critical/2026/10/24/{KEY32}/07-failed.json")
    assert found == MergedOutput("failed", source, "critical", date(2026, 10, 24), key=KEY32)


def test_a_path_built_by_us_is_read_back_the_same_way():
    day = date(2026, 10, 24)
    assert parse_output_path("urgent", merged_output_path("normal", day, KEY32)).key == KEY32
    assert parse_output_path("batch", merged_output_path("critical", day, KEY32)).kind == "batch_visit"
    jsonl = parse_output_path("batch", merged_output_jsonl_path("normal", day, "normal_000001"))
    assert (jsonl.kind, jsonl.batch_id) == ("batch_jsonl", "normal_000001")


@pytest.mark.parametrize("source,path", [
    ("urgent", f"normal/2026/10/24/{KEY32}/01-input.json"),             # another phase file
    ("urgent", f"normal/2026/10/24/{KEY32}/06-failed.json"),
    ("urgent", "normal/2026/13/45/x/07-failed.json"),                    # a failed file with a date that does not exist
    ("urgent", f"normal/2026/10/24/{KEY32}/07-failed.txt"),
    ("urgent", f"normal/2026/10/24/{KEY32}/07-failed.jsonl"),
    ("urgent", f"normal/2026/10/24/{KEY32}/logs/07-failed.json"),
    ("urgent", f"normal/2026/10/24/{KEY32}/logs/05-output-merged.json"),
    ("urgent", "normal/2026/13/45/x/05-output-merged.json"),             # a date that does not exist
    ("urgent", "normal/2026/10/24/../05-output-merged.json"),
    ("urgent", "high/2026/10/24/x/05-output-merged.json"),               # unknown lane
    ("urgent", "normal-jsonls/2026/10/24/b/05-output-merged.jsonl"),     # a jsonl outside the batch container
    ("batch", "normal-jsonls/2026/10/24/b/05-output-merged.json"),       # a batch folder holds a jsonl
    ("batch", "normal-jsonls/2026/10/24/b/01-input.jsonl"),
    ("walk-in", f"normal/2026/10/24/{KEY32}/05-output-merged.json"),     # unknown source
    ("urgent", "")])
def test_any_other_path_is_not_ours(source, path):
    assert parse_output_path(source, path) is None


def test_a_batch_id_that_is_too_long_to_be_ours_is_not_an_answer():
    assert parse_output_path("batch", f"normal-jsonls/2026/10/24/normal_{'1' * 200}/05-output-merged.jsonl") is None
    ours = parse_output_path("batch", "normal-jsonls/2026/10/24/normal_000001/05-output-merged.jsonl")
    assert ours.batch_id == "normal_000001"
