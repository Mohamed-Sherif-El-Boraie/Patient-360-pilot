import pytest

from patient360.config.settings import REQUIRED, SettingsError, load_settings

SECRET = "super-secret-value-123"
GOOD = {
    "PGHOST": "db.example", "PGDATABASE": "patient360", "PGUSER": "app", "PGPASSWORD": "pg-password-xyz",
    "AZURE_STORAGE_ACCOUNT_URL": "https://acct.blob.core.windows.net",
    "BLOB_INPUT_CONTAINER": "ai-input", "BLOB_OUTPUT_CONTAINER": "ai-output", "IDEMPOTENCY_KEY_SECRET": SECRET,
}


def env(**changes):
    merged = {**GOOD, **changes}
    return {name: value for name, value in merged.items() if value is not None}


def test_all_required_settings_give_a_settings_object():
    settings = load_settings(env())
    assert (settings.pg_host, settings.pg_database) == ("db.example", "patient360")
    assert settings.blob_input_container == "ai-input"
    assert settings.pg_port == 5432 and settings.pg_ssl_mode == "verify-full"
    assert settings.backend_connect_timeout_s == 5.0 and settings.backend_read_timeout_s == 30.0


def test_production_is_the_default_and_logs_info_as_json():
    settings = load_settings(env())
    assert (settings.app_env, settings.log_level, settings.log_format) == ("production", "INFO", "json")


def test_development_logs_debug_in_a_readable_console_format():
    settings = load_settings(env(APP_ENV="development"))
    assert (settings.app_env, settings.log_level, settings.log_format) == ("development", "DEBUG", "console")


def test_pilot_logs_like_production():
    settings = load_settings(env(APP_ENV="pilot"))
    assert (settings.app_env, settings.log_level, settings.log_format) == ("pilot", "INFO", "json")


def test_telemetry_is_off_unless_it_is_switched_on():
    assert load_settings(env()).telemetry_enabled is False
    assert load_settings(env(TELEMETRY_ENABLED="TRUE")).telemetry_enabled is True
    assert load_settings(env(TELEMETRY_ENABLED="false", APP_ENV="production")).telemetry_enabled is False


def test_an_explicit_level_and_format_win_over_the_defaults():
    settings = load_settings(env(APP_ENV="production", LOG_LEVEL="debug", LOG_FORMAT="CONSOLE"))
    assert (settings.log_level, settings.log_format) == ("DEBUG", "console")
    assert load_settings(env(APP_ENV="development", LOG_LEVEL="warning")).log_level == "WARNING"


def test_every_missing_name_is_listed_and_no_value_is_printed():
    with pytest.raises(SettingsError) as error:
        load_settings({"PGPASSWORD": "pg-password-xyz"})
    message = str(error.value)
    assert all(name in message for name in REQUIRED if name != "PGPASSWORD")
    assert "pg-password-xyz" not in message


def test_a_blank_value_counts_as_missing_and_spaces_are_trimmed():
    with pytest.raises(SettingsError, match="PGHOST is missing"):
        load_settings(env(PGHOST="   "))
    assert load_settings(env(PGHOST="  db.example  ")).pg_host == "db.example"


@pytest.mark.parametrize("name,bad", [("APP_ENV", "staging"), ("LOG_LEVEL", "TRACE"), ("LOG_FORMAT", "xml"),
                                      ("TELEMETRY_ENABLED", "maybe"), ("PGPORT", "abc"), ("PGPORT", "70000"),
                                      ("PGPORT", "0"), ("BACKEND_READ_TIMEOUT_S", "-3"),
                                      ("BACKEND_CONNECT_TIMEOUT_S", "fast")])
def test_a_wrong_value_is_refused_by_name(name, bad):
    with pytest.raises(SettingsError, match=name):
        load_settings(env(**{name: bad}))


def test_a_short_secret_is_refused_without_printing_it():
    with pytest.raises(SettingsError) as error:
        load_settings(env(IDEMPOTENCY_KEY_SECRET="short"))
    assert "IDEMPOTENCY_KEY_SECRET" in str(error.value) and "short" not in str(error.value).replace("too short", "")


def test_secrets_never_appear_in_the_text_of_the_settings():
    text = repr(load_settings(env()))
    assert SECRET not in text and "pg-password-xyz" not in text and "pg_password" not in text


def test_the_pgbouncer_port_and_the_timeouts_can_be_set():
    settings = load_settings(env(PGPORT="6432", BACKEND_CONNECT_TIMEOUT_S="2.5", BACKEND_READ_TIMEOUT_S="60"))
    assert settings.pg_port == 6432
    assert (settings.backend_connect_timeout_s, settings.backend_read_timeout_s) == (2.5, 60.0)


def test_aws_and_backend_settings_are_optional_until_they_are_used():
    settings = load_settings(env())
    assert settings.aws_region is None and settings.s3_buckets == () and settings.backend_results_url is None
    full = load_settings(env(AWS_REGION="eu-west-1", S3_BUCKET_URGENT="urgent-bucket", S3_BUCKET_BATCH="batch-bucket",
                             BACKEND_RESULTS_URL="https://backend.example/results"))
    assert full.s3_buckets == (("urgent", "urgent-bucket"), ("batch", "batch-bucket"))
    assert full.aws_region == "eu-west-1"
    assert full.backend_results_url == "https://backend.example/results"
