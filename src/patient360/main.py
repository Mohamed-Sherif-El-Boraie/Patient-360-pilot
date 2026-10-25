"""The uvicorn entry point: ``uvicorn --factory patient360.main:create_app_from_env``."""
from __future__ import annotations

from fastapi import FastAPI

from patient360.config.logging_setup import configure_logging
from patient360.config.settings import load_settings
from patient360.http_app import create_app
from patient360.runtime import build_services
from patient360.timers import lifespan


def create_app_from_env() -> FastAPI:
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_format)
    services = build_services(settings)
    return create_app(services, lifespan=lifespan(services))
