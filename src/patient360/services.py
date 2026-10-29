"""The running service as one object: the flow classes, already connected to their stores. Built once at start-up
(``runtime.build_services``) and handed to the HTTP routes and the timers."""
from __future__ import annotations

from dataclasses import dataclass

from patient360.adapters.postgres.db import Database
from patient360.config.settings import Settings
from patient360.flow.batching import BatchTrigger
from patient360.flow.dispatch import Dispatcher
from patient360.flow.intake import Intake
from patient360.flow.process import Processor
from patient360.flow.results import OutputReceiver
from patient360.flow.send_result import ResultSender
from patient360.flow.symptom import SymptomProcessor
from patient360.ports import BackendClient, VisitStore


@dataclass(frozen=True)
class Services:
    settings: Settings
    db: Database                          # only for the health check
    store: VisitStore                     # for the sweeps that look for work cut short by a restart
    intake: Intake
    processor: Processor
    symptom_processor: SymptomProcessor
    batch_trigger: BatchTrigger
    dispatcher: Dispatcher
    receiver: OutputReceiver
    sender: ResultSender | None           # None when the Backend settings are not given: nothing is delivered
    backend: BackendClient | None = None  # the same client, for the best-effort "failed" message of a rejected payload
