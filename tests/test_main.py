"""The entry point: settings from the environment, logging, services, the app."""
import pytest
from fastapi import FastAPI

from patient360 import main
from patient360.config.settings import SettingsError

ENV = {"PGHOST": "127.0.0.1", "PGDATABASE": "patient360", "PGUSER": "app", "PGPASSWORD": "pg-password-xyz",
       "AZURE_STORAGE_ACCOUNT_URL": "https://acct.blob.core.windows.net", "BLOB_INPUT_CONTAINER": "ai-input",
       "IDEMPOTENCY_KEY_SECRET": "super-secret-value-123"}


def test_the_app_is_built_from_the_environment(monkeypatch):
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    seen = {}

    def fake_build_services(settings):
        seen["settings"] = settings
        return "the services"

    monkeypatch.setattr(main, "build_services", fake_build_services)
    monkeypatch.setattr(main, "lifespan", lambda services: None)
    monkeypatch.setattr(main, "create_app", lambda services, lifespan=None: FastAPI())
    assert isinstance(main.create_app_from_env(), FastAPI)
    assert seen["settings"].pg_host == "127.0.0.1"


def test_a_wrong_setting_stops_the_start_and_names_only_the_setting(monkeypatch):
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("BATCH_SIZE", "7")
    with pytest.raises(SettingsError, match="BATCH_SIZE") as error:
        main.create_app_from_env()
    assert "pg-password-xyz" not in str(error.value)
