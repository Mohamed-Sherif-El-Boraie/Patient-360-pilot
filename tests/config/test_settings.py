import pytest

from patient360.config.settings import REQUIRED, SettingsError, load_settings

SECRET = "super-secret-value-123"
GOOD = {
    "PGHOST": "db.example", "PGDATABASE": "patient360", "PGUSER": "app", "PGPASSWORD": "pg-password-xyz",
    "AZURE_STORAGE_ACCOUNT_URL": "https://acct.blob.core.windows.net",
    "BLOB_INPUT_CONTAINER": "ai-input", "IDEMPOTENCY_KEY_SECRET": SECRET,
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


def test_the_raw_sas_url_replaces_the_account_and_the_container_and_is_never_shown():
    url = "https://acct.blob.core.windows.net/p360-raw?sig=TOKEN-VALUE"
    settings = load_settings(env(AZURE_STORAGE_ACCOUNT_URL=None, BLOB_INPUT_CONTAINER=None, BLOB_OUTPUT_URL_RAW=url,
                                 BLOB_OUTPUT_URL_URGENT=url))
    assert settings.blob_raw_url == url and settings.output_container_urls == (("urgent", url),)
    assert "TOKEN-VALUE" not in repr(settings)
    with pytest.raises(SettingsError, match="BLOB_INPUT_CONTAINER"):
        load_settings(env(BLOB_INPUT_CONTAINER=None))


def test_the_schema_is_optional_and_must_be_a_plain_lower_case_name():
    assert load_settings(env()).pg_schema is None
    assert load_settings(env(PGSCHEMA="p360")).pg_schema == "p360"
    with pytest.raises(SettingsError, match="PGSCHEMA"):
        load_settings(env(PGSCHEMA='p360"; DROP TABLE visits; --'))


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

BACKEND = {"BACKEND_BASE_URL": "https://wrapper.example/", "BACKEND_CALLER_SOURCE": "Patient360",
           "BACKEND_CALLER_SOURCE_KEY": "caller-key-value", "BACKEND_CALLER_USER_NAME": "Patient360",
           "BACKEND_USERNAME": "api-user", "BACKEND_PASSWORD": "api-password-xyz"}


def test_aws_and_backend_settings_are_optional_until_they_are_used():
    settings = load_settings(env())
    assert settings.aws_region is None and settings.s3_buckets == () and settings.backend is None
    assert settings.aws_endpoint_url is None
    full = load_settings(env(AWS_REGION="eu-west-1", S3_BUCKET_URGENT="urgent-bucket", S3_BUCKET_BATCH="batch-bucket",
                             AWS_ENDPOINT_URL_S3="http://minio:9000"))
    assert full.s3_buckets == (("urgent", "urgent-bucket"), ("batch", "batch-bucket"))
    assert full.aws_region == "eu-west-1" and full.aws_endpoint_url == "http://minio:9000"


def test_the_backend_settings_come_all_together_and_the_secrets_stay_out_of_the_text():
    backend = load_settings(env(**BACKEND)).backend
    assert (backend.base_url, backend.username) == ("https://wrapper.example", "api-user")
    assert "api-password-xyz" not in repr(backend) and "caller-key-value" not in repr(backend)
    partial = {name: value for name, value in BACKEND.items() if name != "BACKEND_PASSWORD"}
    with pytest.raises(SettingsError, match="BACKEND_PASSWORD") as error:
        load_settings(env(**partial))
    assert "api-user" not in str(error.value)


def test_the_blob_account_url_or_the_emulator_connection_string_is_enough_but_one_is_needed():
    assert load_settings(env()).azure_storage_account_url == "https://acct.blob.core.windows.net"
    local = load_settings(
        env(AZURE_STORAGE_ACCOUNT_URL=None, AZURE_STORAGE_CONNECTION_STRING="UseDevelopmentStorage=true"))
    assert local.azure_storage_account_url is None and local.azure_storage_connection_string
    assert "UseDevelopmentStorage" not in repr(local)
    with pytest.raises(SettingsError, match="AZURE_STORAGE_ACCOUNT_URL or AZURE_STORAGE_CONNECTION_STRING"):
        load_settings(env(AZURE_STORAGE_ACCOUNT_URL=None))


def test_the_inbound_key_and_the_two_intervals():
    default = load_settings(env())
    assert (default.inbound_api_key, default.job_interval_s, default.work_interval_s) == (None, 300, 5)
    custom = load_settings(env(INBOUND_API_KEY="shared-key-123", JOB_INTERVAL_S="60", WORK_INTERVAL_S="2.5"))
    assert (custom.inbound_api_key, custom.job_interval_s, custom.work_interval_s) == ("shared-key-123", 60, 2.5)
    assert "shared-key-123" not in repr(custom)
    for name in ("JOB_INTERVAL_S", "WORK_INTERVAL_S"):
        with pytest.raises(SettingsError, match=name):
            load_settings(env(**{name: "0"}))

