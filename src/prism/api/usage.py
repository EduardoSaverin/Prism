"""Settle a finished request's budgets and pass its usage to the recorder."""

from __future__ import annotations

import logging

from fastapi import Request

from prism.core.context import RequestContext
from prism.core.errors import ErrorKind, PrismError
from prism.core.governance.budgets import BudgetTracker
from prism.core.usage.base import UsageRecorder
from prism.core.usage.record import STATUS_CANCELLED, STATUS_SUCCESS, usage_record

log = logging.getLogger(__name__)


def status_of(error: BaseException | None) -> str:
    """The usage status for how a request ended: "success", an error kind, or "cancelled"."""
    if error is None:
        return STATUS_SUCCESS
    if isinstance(error, PrismError):
        return error.kind.value
    if isinstance(error, Exception):
        return ErrorKind.INTERNAL_ERROR.value
    return STATUS_CANCELLED  # CancelledError or GeneratorExit: the client went away


def record_usage(request: Request, ctx: RequestContext, error: BaseException | None) -> None:
    """Charge live budgets independently of the best-effort usage record."""
    budgets: BudgetTracker | None = getattr(request.app.state, "budgets", None)
    threshold = budgets.charge(ctx) if budgets is not None else None
    recorder: UsageRecorder | None = getattr(request.app.state, "usage", None)
    if recorder is None:
        return
    try:
        recorder.record(usage_record(ctx, status_of(error), budget_threshold=threshold))
    except Exception:
        log.exception("Could not record usage for request %s", ctx.request_id)
