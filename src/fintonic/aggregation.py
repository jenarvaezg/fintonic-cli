"""Bank sync ("Actualizar entidades") status, mirroring the web app's polling rules.

After `POST {BFF}/openbanking/aggregations/data-actualizations` the web app polls
`GET {BFF}/openbanking/aggregations/` every second until no aggregation is
`IN_PROGRESS` (except those waiting for the user's 2FA, which never finish on their own).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

ERROR_STATES = frozenset(
    {"ACTIVE_ERROR", "PASSIVE_ERROR", "ONBOARDING_WITH_ACTIVE_ERROR", "ONBOARDING_WITH_PASSIVE_ERROR"}
)


@dataclass(frozen=True)
class AggregationStatus:
    in_progress: tuple[str, ...]  # entity (bank) ids still syncing
    needs_2fa: tuple[str, ...]  # waiting for the user to approve in the bank app
    errors: tuple[str, ...]

    @property
    def done(self) -> bool:
        return not self.in_progress


def evaluate(aggregations: Sequence[dict[str, Any]]) -> AggregationStatus:
    def entities(pred) -> tuple[str, ...]:
        return tuple(str(a.get("entityId")) for a in aggregations if pred(a.get("status") or {}))

    return AggregationStatus(
        in_progress=entities(lambda s: s.get("general") == "IN_PROGRESS" and s.get("detail") != "2FA"),
        needs_2fa=entities(lambda s: s.get("general") == "IN_PROGRESS" and s.get("detail") == "2FA"),
        errors=entities(lambda s: s.get("general") in ERROR_STATES),
    )
