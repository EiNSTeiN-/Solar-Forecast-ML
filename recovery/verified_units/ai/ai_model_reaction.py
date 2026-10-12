# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
"""Govern confirmed drift reactions without bypassing model promotion gates."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ModelReactionOutcome:
    """Result of processing one persistent model reaction request."""

    status: str
    reason: str
    request_id: Optional[str] = None
    candidate_id: Optional[str] = None


class ModelReactionHandler:
    """Bind confirmed drift to a current candidate or replace a stale one."""

    def __init__(self, db_manager: Any) -> None:
        self._db = db_manager

    def set_db_manager(self, db_manager: Any) -> None:
        """Rebind the initialized database service."""
        self._db = db_manager

    async def process(self, through_date: str, predictor: Any) -> ModelReactionOutcome:
        """Process at most one coalesced, restart-safe model reaction."""
        request = await self._db.get_or_create_confirmed_model_retrain_request(
            through_date
        )
        if request is None:
            return ModelReactionOutcome(
                status="not_required", reason="no confirmed multi-day model drift"
            )
        request_id = request["request_id"]
        if request["status"] == "bound":
            return ModelReactionOutcome(
                status="bound",
                reason="confirmed drift is already assigned to a model candidate",
                request_id=request_id,
                candidate_id=request.get("bound_candidate_id"),
            )
        candidate_id = await self._db.get_pending_model_candidate_id()
        if not candidate_id:
            return ModelReactionOutcome(
                status="deferred",
                reason="confirmed drift awaits a successfully trained candidate",
                request_id=request_id,
            )
        if await self._db.model_candidate_covers_retrain_request(
            request_id, candidate_id
        ):
            await self._db.bind_model_retrain_request(request_id, candidate_id)
            return ModelReactionOutcome(
                status="bound",
                reason="current candidate already contains the confirmed drift data",
                request_id=request_id,
                candidate_id=candidate_id,
            )
        training_result = await predictor.train_model_for_reaction(
            request_id, candidate_id, str(request["last_event_date"])
        )
        if not training_result.success:
            return ModelReactionOutcome(
                status="failed",
                reason=training_result.error_message
                or "replacement candidate training failed",
                request_id=request_id,
                candidate_id=candidate_id,
            )
        return ModelReactionOutcome(
            status="replaced",
            reason="stale candidate replaced after confirmed multi-day drift",
            request_id=request_id,
            candidate_id=getattr(predictor, "_latest_model_candidate_id", None),
        )
