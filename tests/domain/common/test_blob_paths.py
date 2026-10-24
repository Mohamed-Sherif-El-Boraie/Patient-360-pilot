from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from patient360.domain.common.blob_paths import (
    MergedOutput,
    batch_jsonl_path,
    blob_day,
    merged_output_jsonl_path,
    merged_output_path,
    parse_merged_output_path,
    processed_path,
    raw_path,
    rejected_output_path,
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


def test_the_merged_output_lives_in_blob_2():
    assert merged_output_path("urgent", "normal", DAY, KEY) == (
        f"aws-output-merged/urgent/normal/2026/10/24/{FILE}")
    assert merged_output_path("batch", "critical", DAY, KEY, batch_id="critical_000007") == (
        f"aws-output-merged/batch/critical/2026/10/24/critical_000007/{FILE}")
    assert merged_output_jsonl_path("normal", DAY, "normal_000001") == (
        "aws-output-merged/batch/normal-jsonls/2026/10/24/normal_000001.jsonl")


def test_the_s3_key_is_inside_the_bucket_of_the_source():
    assert s3_processed_key("urgent", "critical", DAY, KEY) == f"processed/critical/2026/10/24/{FILE}"
    assert s3_processed_key("batch", "normal", DAY, KEY, batch_id="normal_000001") == (
        "processed/normal-jsonls/2026/10/24/normal_000001.jsonl")


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
    assert merged_output_path("urgent", "normal", day, KEY).split("/")[3:6] == ["2026", "10", "24"]


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


def test_a_blob_2_path_is_read_back_into_its_parts():
    visit = parse_merged_output_path(f"aws-output-merged/on-demand/critical/2026/10/24/{KEY32}.json")
    assert visit == MergedOutput("visit", "on-demand", "critical", date(2026, 10, 24), key=KEY32)
    jsonl = parse_merged_output_path("aws-output-merged/batch/normal-jsonls/2026/10/25/normal_000001.jsonl")
    assert jsonl == MergedOutput("batch_jsonl", "batch", "normal", date(2026, 10, 25), batch_id="normal_000001")
    split = parse_merged_output_path(f"aws-output-merged/batch/critical/2026/10/25/critical_000002/{KEY32}.json")
    assert split == MergedOutput("batch_visit", "batch", "critical", date(2026, 10, 25), key=KEY32,
                                 batch_id="critical_000002")


def test_a_path_built_by_us_is_read_back_the_same_way():
    day = date(2026, 10, 24)
    assert parse_merged_output_path(merged_output_path("urgent", "normal", day, KEY32)).key == KEY32
    path = merged_output_path("batch", "critical", day, KEY32, "critical_000003")
    assert parse_merged_output_path(path).batch_id == "critical_000003"
    assert parse_merged_output_path(merged_output_jsonl_path("normal", day, "normal_000001")).kind == "batch_jsonl"


@pytest.mark.parametrize("path", [
    "processed/urgent/normal/2026/10/24/" + KEY32 + ".json", "aws-output-merged/urgent/normal/2026/13/45/x.json",
    "aws-output-merged/urgent/normal/2026/10/24/../x.json", "aws-output-merged/other/normal/2026/10/24/x.json",
    "aws-output-merged/urgent/normal/2026/10/24/x.txt", ""])
def test_any_other_path_is_not_ours(path):
    assert parse_merged_output_path(path) is None


def test_a_rejected_output_is_named_by_its_key_or_by_a_hash_of_its_text():
    day = date(2026, 10, 24)
    named_by_key = f"aws-output-merged/_rejected/urgent/2026/10/24/{KEY32}.json"
    assert rejected_output_path("urgent", day, "{}", KEY32) == named_by_key
    for bad_key in (None, 5, "../x", ""):
        path = rejected_output_path("batch", day, "some text", bad_key)
        assert path.startswith("aws-output-merged/_rejected/batch/2026/10/24/unknown_") and path.endswith(".json")
    assert parse_merged_output_path(rejected_output_path("urgent", day, "{}", KEY32)) is None   # never handled again

