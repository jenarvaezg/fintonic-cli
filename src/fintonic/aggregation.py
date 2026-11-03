"""Bank sync ("Actualizar entidades") status, mirroring the apps' polling rules.

After `POST {BFF}/openbanking/aggregations/data-actualizations` (or
`/openbanking/aggregations/{id}/data-actualizations` for one bank) the web app polls
`GET {BFF}/openbanking/aggregations/` every second until no aggregation is
`IN_PROGRESS` (except those waiting for the user's 2FA, which never finish on their own).

The 2FA rules come from the Android app (11.9.1.0): an aggregation waits for a second
factor when `status.detail == "2FA"`; `status.reason` tells an SMS code
(`WAITING_PARAM`) from a push approval in the bank app (`BACKGROUND_*`), and a set of
reasons (or `detail == "INVALID_2FA"`) means the last answer was rejected or expired.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

from fintonic.errors import InputError

ERROR_STATES = frozenset(
    {"ACTIVE_ERROR", "PASSIVE_ERROR", "ONBOARDING_WITH_ACTIVE_ERROR", "ONBOARDING_WITH_PASSIVE_ERROR"}
)
INVALID_2FA_REASONS = frozenset(
    {
        "INVALID_WAITING_PARAM",
        "INVALID_BACKGROUND_WAITING_PARAM",
        "BACKGROUND_INVALID_WAITING_PARAM",
        "TIMEOUT_WAITING_PARAM",
        "TIMEOUT_BACKGROUND_WAITING_PARAM",
        "CANCELLED_WAITING_PARAM",
        "CANCELLED_BACKGROUND_WAITING_PARAM",
        "INVALID_DYNAMIC",
        "BACKGROUND_INVALID_DYNAMIC",
    }
)
PUSH_2FA_REASONS = frozenset(
    {
        "BACKGROUND_WAITING_PARAM",
        "INVALID_BACKGROUND_WAITING_PARAM",
        "BACKGROUND_INVALID_WAITING_PARAM",
        "TIMEOUT_BACKGROUND_WAITING_PARAM",
        "CANCELLED_BACKGROUND_WAITING_PARAM",
        "BACKGROUND_INVALID_DYNAMIC",
    }
)


def _status(aggregation: dict[str, Any]) -> dict[str, Any]:
    return aggregation.get("status") or {}


def _upper(value: Any) -> str:
    return str(value or "").strip().upper()


def entity_id(aggregation: dict[str, Any]) -> str:
    return str(aggregation.get("entityId"))


def is_syncing(aggregation: dict[str, Any]) -> bool:
    return _upper(_status(aggregation).get("general")) == "IN_PROGRESS"


def is_invalid_2fa(aggregation: dict[str, Any]) -> bool:
    status = _status(aggregation)
    return _upper(status.get("detail")) == "INVALID_2FA" or _upper(status.get("reason")) in INVALID_2FA_REASONS


def needs_2fa(aggregation: dict[str, Any]) -> bool:
    """Waiting for a (first or retried) second factor from the user."""
    status = _status(aggregation)
    return _upper(status.get("detail")) == "2FA" and not is_invalid_2fa(aggregation)


@dataclass(frozen=True)
class AggregationStatus:
    in_progress: tuple[str, ...]  # entity (bank) ids still syncing
    needs_2fa: tuple[str, ...]  # waiting for the user's code or approval in the bank app
    errors: tuple[str, ...]
    invalid_2fa: tuple[str, ...] = ()  # last 2FA answer rejected, expired or cancelled
    unconfirmed: tuple[str, ...] = ()  # ended without error but never seen syncing after the refresh

    @property
    def failed(self) -> bool:
        """Some bank ended without a confirmed sync (error, 2FA pending or rejected, or unconfirmed)."""
        return bool(self.errors or self.needs_2fa or self.invalid_2fa or self.unconfirmed)

    @property
    def done(self) -> bool:
        return not self.in_progress


def evaluate(aggregations: Sequence[dict[str, Any]], *, awaiting: Collection[str] = ()) -> AggregationStatus:
    """Classify each aggregation once. Banks in `awaiting` just got a 2FA answer and count as syncing."""
    groups: dict[str, list[str]] = {"in_progress": [], "needs_2fa": [], "errors": [], "invalid_2fa": []}
    for agg in aggregations:
        if entity_id(agg) in awaiting:
            key = "in_progress"
        elif is_invalid_2fa(agg):
            key = "invalid_2fa"
        elif needs_2fa(agg):
            key = "needs_2fa"
        elif _upper(_status(agg).get("general")) in ERROR_STATES:
            key = "errors"
        elif is_syncing(agg):
            key = "in_progress"
        else:
            continue
        groups[key].append(entity_id(agg))
    return AggregationStatus(**{key: tuple(ids) for key, ids in groups.items()})


def find_aggregation(aggregations: Sequence[dict[str, Any]], bank_id: str) -> dict[str, Any]:
    """Map a bank id (`entityId`, as in `fintonic banks`) to its open-banking aggregation."""
    for agg in aggregations:
        if entity_id(agg) == bank_id:
            return agg
    known = ", ".join(sorted(entity_id(a) for a in aggregations)) or "ninguno"
    raise InputError(f"No hay ningún banco conectado con id {bank_id}. Bancos: {known}.")


@dataclass(frozen=True)
class SecondFactorRequest:
    """What the bank asks for, from the aggregation's `aggregationWay` (texts come from Fintonic)."""

    bank_id: str
    aggregation_id: str
    push: bool  # approve in the bank app (answer is a confirmation) instead of typing a code
    retry: bool  # the previous answer was rejected or expired
    message: str = ""
    placeholder: str = ""

    @classmethod
    def from_aggregation(cls, aggregation: dict[str, Any]) -> SecondFactorRequest:
        way = aggregation.get("aggregationWay") or {}
        return cls(
            bank_id=entity_id(aggregation),
            aggregation_id=str(aggregation.get("id")),
            push=_upper(_status(aggregation).get("reason")) in PUSH_2FA_REASONS,
            retry=is_invalid_2fa(aggregation),
            message=str(way.get("mfaMessage") or "").strip(),
            placeholder=str(way.get("mfaPlaceholder") or way.get("mfaPlaceHolder") or "").strip(),
        )


def second_factor_body(otp: str | None) -> dict[str, str]:
    """`PUT .../2fa` body: `{"otp": ...}` for a code; `{}` confirms a push approval (app sends otp=null)."""
    otp = (otp or "").strip()
    return {"otp": otp} if otp else {}
