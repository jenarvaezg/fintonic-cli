"""Single-bank refresh and the bank's 2FA (issue #6). Synthetic aggregations only."""

import json
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from fintonic import cli
from fintonic.aggregation import SecondFactorRequest, evaluate, find_aggregation, second_factor_body
from fintonic.client import MAX_2FA_ATTEMPTS, FintonicClient
from fintonic.errors import InputError
from fintonic.session import Session
from fintonic.stores import MemorySessionStore
from tests.conftest import API, BFF

runner = CliRunner()
AGGS = f"{BFF}/openbanking/aggregations/"
WAY = {"mfaMessage": "Introduce el código que te ha enviado tu banco.", "mfaPlaceHolder": "Código del SMS"}


def agg(entity: str, general: str = "COMPLETED", detail: str = "OK", reason: str | None = None) -> dict[str, Any]:
    status: dict[str, Any] = {"general": general, "detail": detail}
    if reason is not None:
        status["reason"] = reason
    return {"id": f"agg-{entity}", "entityId": entity, "status": status, "aggregationWay": WAY}


def sms_2fa(entity: str) -> dict[str, Any]:
    return agg(entity, "IN_PROGRESS", "2FA", "WAITING_PARAM")


def registries(*dates: dict[str, int | None]) -> list[httpx.Response]:
    """Successive `GET /rest/bankRegistries` answers: bank id -> lastUpdateDate (epoch ms)."""
    return [
        httpx.Response(
            200,
            json={"data": {"bankRegistries": [{"bankId": b, "lastUpdateDate": d} for b, d in snapshot.items()]}},
        )
        for snapshot in dates
    ]


def mock_registries(mock_api: respx.MockRouter, *dates: dict[str, int | None]) -> respx.Route:
    """Mock bankRegistries; the last snapshot repeats forever."""
    answers = registries(*dates)
    return mock_api.get(f"{API}/rest/bankRegistries").mock(
        side_effect=lambda _request: answers.pop(0) if len(answers) > 1 else answers[0]
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _body(route: respx.Route, index: int = -1) -> Any:
    content = route.calls[index].request.content
    return json.loads(content) if content else None


# ---- status rules ------------------------------------------------------------


def test_evaluate_classifies_each_bank_once() -> None:
    status = evaluate(
        [
            agg("0182", "IN_PROGRESS"),
            sms_2fa("2091"),
            agg("1465", "ACTIVE_ERROR", "OK", "TIMEOUT_WAITING_PARAM"),
            agg("0049", "IN_PROGRESS", "INVALID_2FA"),
            agg("0081", "COMPLETED"),
        ]
    )
    assert status.in_progress == ("0182",)
    assert status.needs_2fa == ("2091",)
    assert status.invalid_2fa == ("1465", "0049")
    assert status.errors == ()


def test_evaluate_treats_answered_banks_as_syncing() -> None:
    status = evaluate([sms_2fa("2091")], awaiting={"2091"})
    assert status.in_progress == ("2091",)
    assert not status.needs_2fa


def test_second_factor_request_modes() -> None:
    sms = SecondFactorRequest.from_aggregation(sms_2fa("2091"))
    assert (sms.push, sms.retry, sms.aggregation_id) == (False, False, "agg-2091")
    assert sms.placeholder == "Código del SMS"
    assert sms.message.startswith("Introduce")

    push = SecondFactorRequest.from_aggregation(agg("2091", "IN_PROGRESS", "2FA", "BACKGROUND_WAITING_PARAM"))
    assert push.push and not push.retry

    retry = SecondFactorRequest.from_aggregation(agg("2091", "IN_PROGRESS", "2FA", "INVALID_WAITING_PARAM"))
    assert retry.retry and not retry.push


def test_second_factor_body() -> None:
    assert second_factor_body(" 123456 ") == {"otp": "123456"}
    assert second_factor_body("") == {}
    assert second_factor_body(None) == {}


def test_find_aggregation_unknown_bank() -> None:
    with pytest.raises(InputError, match="0182"):
        find_aggregation([agg("0182")], "9999")


# ---- client ------------------------------------------------------------------


def test_refresh_single_bank(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(AGGS).mock(return_value=httpx.Response(200, json=[agg("0182"), agg("2091")]))
    one = mock_api.post(f"{BFF}/openbanking/aggregations/agg-2091/data-actualizations").mock(
        return_value=httpx.Response(202)
    )
    every = mock_api.post(f"{BFF}/openbanking/aggregations/data-actualizations")

    FintonicClient(session, http=http).refresh_bank("2091")

    assert one.call_count == 1
    assert not every.called


def test_refresh_unknown_bank_posts_nothing(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(AGGS).mock(return_value=httpx.Response(200, json=[agg("0182")]))
    with pytest.raises(InputError):
        FintonicClient(session, http=http).refresh_bank("2091")
    assert all(call.request.method == "GET" for call in mock_api.calls)


def test_submit_second_factor(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.put(f"{BFF}/openbanking/aggregations/agg-2091/2fa").mock(return_value=httpx.Response(200))
    client = FintonicClient(session, http=http)
    client.submit_second_factor("agg-2091", "123456")
    client.submit_second_factor("agg-2091", None)
    assert _body(route, 0) == {"otp": "123456"}
    assert _body(route, 1) == {}


def test_submit_second_factor_is_not_retried(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.put(f"{BFF}/openbanking/aggregations/agg-2091/2fa").mock(return_value=httpx.Response(503))
    with pytest.raises(Exception, match="503"):
        FintonicClient(session, http=http).submit_second_factor("agg-2091", "1")
    assert route.call_count == 1


def test_wait_answers_2fa_and_keeps_waiting(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(AGGS).mock(
        side_effect=[
            httpx.Response(200, json=[sms_2fa("2091")]),
            httpx.Response(200, json=[sms_2fa("2091")]),  # not processed yet: still waiting, don't ask again
            httpx.Response(200, json=[agg("2091", "IN_PROGRESS")]),
            httpx.Response(200, json=[agg("2091")]),
        ]
    )
    put = mock_api.put(f"{BFF}/openbanking/aggregations/agg-2091/2fa").mock(return_value=httpx.Response(200))
    asked: list[SecondFactorRequest] = []

    def answer(request: SecondFactorRequest) -> str:
        asked.append(request)
        return "654321"

    status = FintonicClient(session, http=http).wait_for_banks(sleep=lambda _: None, on_2fa=answer)

    assert status.done and not status.needs_2fa
    assert [r.bank_id for r in asked] == ["2091"]
    assert _body(put) == {"otp": "654321"}


def test_wait_skipped_2fa_is_reported(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(AGGS).mock(
        side_effect=[
            httpx.Response(200, json=[sms_2fa("2091"), agg("0182", "IN_PROGRESS")]),
            httpx.Response(200, json=[sms_2fa("2091"), agg("0182")]),
        ]
    )
    put = mock_api.put(f"{BFF}/openbanking/aggregations/agg-2091/2fa")
    asked: list[str] = []

    def skip(request: SecondFactorRequest) -> None:
        asked.append(request.bank_id)

    status = FintonicClient(session, http=http).wait_for_banks(sleep=lambda _: None, on_2fa=skip)

    assert asked == ["2091"]
    assert status.needs_2fa == ("2091",)
    assert not put.called


def test_wait_retries_rejected_code_up_to_limit(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    rejected = [agg("2091", "IN_PROGRESS", "INVALID_2FA", f"INVALID_WAITING_PARAM_{i}") for i in range(5)]
    mock_api.get(AGGS).mock(side_effect=[httpx.Response(200, json=[a]) for a in [sms_2fa("2091"), *rejected]])
    put = mock_api.put(f"{BFF}/openbanking/aggregations/agg-2091/2fa").mock(return_value=httpx.Response(200))
    retries: list[bool] = []

    def answer(request: SecondFactorRequest) -> str:
        retries.append(request.retry)
        return "000000"

    status = FintonicClient(session, http=http).wait_for_banks(sleep=lambda _: None, on_2fa=answer)

    assert put.call_count == MAX_2FA_ATTEMPTS
    assert retries == [False, True, True]
    assert status.invalid_2fa == ("2091",)


def test_wait_only_for_selected_banks(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(AGGS).mock(return_value=httpx.Response(200, json=[agg("0182", "IN_PROGRESS"), agg("2091")]))
    status = FintonicClient(session, http=http).wait_for_banks(bank_ids=["2091"], sleep=lambda _: None)
    assert status.done


# ---- did the refresh really sync? (issue #20) ----------------------------------


_REAL_WAIT = FintonicClient.wait_for_banks  # the autouse `sleeps` fixture below swaps in its own clock


def _wait_since(client: FintonicClient, since: dict[str, int | None], clock: FakeClock) -> Any:
    return _REAL_WAIT(client, since=since, interval=2, grace=20, sleep=clock.sleep, clock=clock)


def test_wait_ignores_final_state_from_before_the_refresh(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    mock_api.get(AGGS).mock(
        side_effect=[
            httpx.Response(200, json=[agg("2091")]),  # previous sync, refresh not picked up yet
            httpx.Response(200, json=[agg("2091", "IN_PROGRESS")]),
            httpx.Response(200, json=[agg("2091")]),
        ]
    )
    mock_registries(mock_api, {"2091": 1000})
    clock = FakeClock()

    status = _wait_since(FintonicClient(session, http=http), {"2091": 1000}, clock)

    assert status.done and not status.failed
    assert clock.now == 4


def test_wait_accepts_newer_last_update_without_seeing_progress(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    mock_api.get(AGGS).mock(return_value=httpx.Response(200, json=[agg("2091")]))
    mock_registries(mock_api, {"2091": 2000})
    clock = FakeClock()

    status = _wait_since(FintonicClient(session, http=http), {"2091": 1000}, clock)

    assert not status.failed
    assert clock.now == 0


def test_wait_reports_bank_that_never_synced(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(AGGS).mock(return_value=httpx.Response(200, json=[agg("2091"), agg("0182", "ACTIVE_ERROR")]))
    mock_registries(mock_api, {"2091": 1000, "0182": 1000})
    clock = FakeClock()

    status = _wait_since(FintonicClient(session, http=http), {"2091": 1000, "0182": 1000}, clock)

    assert status.unconfirmed == ("2091",)
    assert status.errors == ("0182",)
    assert status.failed
    assert clock.now == 20


def test_wait_with_stale_error_waits_for_the_new_sync(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    mock_api.get(AGGS).mock(
        side_effect=[
            httpx.Response(200, json=[agg("2091", "ACTIVE_ERROR")]),
            httpx.Response(200, json=[agg("2091", "IN_PROGRESS")]),
            httpx.Response(200, json=[agg("2091")]),
        ]
    )
    mock_registries(mock_api, {"2091": 1000})

    status = _wait_since(FintonicClient(session, http=http), {"2091": 1000}, FakeClock())

    assert not status.failed


def test_wait_does_not_take_an_old_rejected_2fa_as_this_sync(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    old = agg("2091", "ACTIVE_ERROR", "OK", "TIMEOUT_WAITING_PARAM")
    mock_api.get(AGGS).mock(
        side_effect=[
            httpx.Response(200, json=[old]),
            httpx.Response(200, json=[agg("2091", "IN_PROGRESS")]),
            httpx.Response(200, json=[agg("2091")]),
        ]
    )
    mock_registries(mock_api, {"2091": 1000})

    status = _wait_since(FintonicClient(session, http=http), {"2091": 1000}, FakeClock())

    assert not status.failed


def test_wait_accepts_bank_without_baseline(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    """An aggregation with no bankRegistries entry can't be confirmed by date: don't fail it forever."""
    mock_api.get(AGGS).mock(return_value=httpx.Response(200, json=[agg("9999")]))
    registry = mock_registries(mock_api, {"2091": 1000})
    clock = FakeClock()

    status = _wait_since(FintonicClient(session, http=http), {"2091": 1000}, clock)

    assert not status.failed
    assert clock.now == 0
    assert not registry.called


def test_wait_stops_checking_dates_after_grace(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    mock_api.get(AGGS).mock(return_value=httpx.Response(200, json=[agg("2091"), agg("0182", "IN_PROGRESS")]))
    registry = mock_registries(mock_api, {"2091": 1000, "0182": 1000})
    clock = FakeClock()

    status = _REAL_WAIT(
        FintonicClient(session, http=http),
        since={"2091": 1000, "0182": 1000},
        timeout=60,
        grace=20,
        sleep=clock.sleep,
        clock=clock,
    )

    assert status.unconfirmed == ("2091",)
    assert registry.call_count == 10  # polls at 0, 2, …, 18 s; none in the remaining 40 s


# ---- CLI ---------------------------------------------------------------------


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli, "_store", lambda: memory)
    return memory


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Run the CLI's polling loop without real sleeps (on a fake clock), recording them."""
    recorded: list[float] = []
    original = FintonicClient.wait_for_banks

    def fast_wait(self: FintonicClient, **kwargs: Any) -> Any:
        clock = FakeClock()

        def sleep(seconds: float) -> None:
            recorded.append(seconds)
            clock.sleep(seconds)

        return original(self, **{**kwargs, "sleep": sleep, "clock": clock})

    monkeypatch.setattr(FintonicClient, "wait_for_banks", fast_wait)
    return recorded


def test_cli_refresh_bank(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(AGGS).mock(return_value=httpx.Response(200, json=[agg("0182", "IN_PROGRESS"), agg("2091")]))
    mock_registries(mock_api, {"0182": 1000, "2091": 1000}, {"0182": 1000, "2091": 2000})
    one = mock_api.post(f"{BFF}/openbanking/aggregations/agg-2091/data-actualizations").mock(
        return_value=httpx.Response(202)
    )

    result = runner.invoke(cli.app, ["refresh", "--bank", "2091"])

    assert result.exit_code == 0, result.output
    assert one.called
    assert "actualizados" in result.stderr


def test_cli_refresh_prompts_for_2fa(mock_api: respx.MockRouter, store: MemorySessionStore, tty: None) -> None:
    mock_registries(mock_api, {"2091": 1000})
    mock_api.post(f"{BFF}/openbanking/aggregations/data-actualizations").mock(return_value=httpx.Response(202))
    mock_api.get(AGGS).mock(
        side_effect=[httpx.Response(200, json=[sms_2fa("2091")]), httpx.Response(200, json=[agg("2091")])]
    )
    put = mock_api.put(f"{BFF}/openbanking/aggregations/agg-2091/2fa").mock(return_value=httpx.Response(200))

    result = runner.invoke(cli.app, ["refresh"], input="123456\n")

    assert result.exit_code == 0, result.output
    assert _body(put) == {"otp": "123456"}
    assert "Introduce el código" in result.stderr
    assert "Código del SMS" in result.stderr


def test_cli_refresh_push_confirmation(mock_api: respx.MockRouter, store: MemorySessionStore, tty: None) -> None:
    push = agg("2091", "IN_PROGRESS", "2FA", "BACKGROUND_WAITING_PARAM")
    mock_registries(mock_api, {"2091": 1000})
    mock_api.post(f"{BFF}/openbanking/aggregations/data-actualizations").mock(return_value=httpx.Response(202))
    mock_api.get(AGGS).mock(side_effect=[httpx.Response(200, json=[push]), httpx.Response(200, json=[agg("2091")])])
    put = mock_api.put(f"{BFF}/openbanking/aggregations/agg-2091/2fa").mock(return_value=httpx.Response(200))

    result = runner.invoke(cli.app, ["refresh"], input="\n")

    assert result.exit_code == 0, result.output
    assert _body(put) == {}


def test_cli_refresh_no_2fa_only_reports(mock_api: respx.MockRouter, store: MemorySessionStore, tty: None) -> None:
    mock_registries(mock_api, {"2091": 1000})
    mock_api.post(f"{BFF}/openbanking/aggregations/data-actualizations").mock(return_value=httpx.Response(202))
    mock_api.get(AGGS).mock(return_value=httpx.Response(200, json=[sms_2fa("2091")]))
    put = mock_api.put(f"{BFF}/openbanking/aggregations/agg-2091/2fa")

    result = runner.invoke(cli.app, ["refresh", "--no-2fa"])

    assert result.exit_code == 3, result.output  # pending 2FA: not synced, scripts must notice
    assert not put.called
    assert "bank-2fa" in result.stderr


def test_cli_refresh_bank_error_exits_non_zero(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_registries(mock_api, {"2091": 1000})
    mock_api.post(f"{BFF}/openbanking/aggregations/data-actualizations").mock(return_value=httpx.Response(202))
    mock_api.get(AGGS).mock(
        side_effect=[
            httpx.Response(200, json=[agg("2091", "IN_PROGRESS")]),
            httpx.Response(200, json=[agg("2091", "ACTIVE_ERROR")]),
        ]
    )

    result = runner.invoke(cli.app, ["refresh"])

    assert result.exit_code == 3, result.output
    assert "Con error: 2091" in result.stderr
    assert "actualizados" not in result.stderr


def test_cli_refresh_never_seen_syncing_is_not_success(
    mock_api: respx.MockRouter, store: MemorySessionStore, sleeps: list[float]
) -> None:
    mock_registries(mock_api, {"2091": 1_767_225_600_000})  # 2026-01-01
    mock_api.post(f"{BFF}/openbanking/aggregations/data-actualizations").mock(return_value=httpx.Response(202))
    mock_api.get(AGGS).mock(return_value=httpx.Response(200, json=[agg("2091")]))

    result = runner.invoke(cli.app, ["refresh"])

    assert result.exit_code == 3, result.output
    assert "Sin constancia" in result.stderr
    assert "2091 (última 2026-01-01" in result.stderr
    assert "actualizados" not in result.stderr
    assert sum(sleeps) >= 20


def test_cli_refresh_timeout_exits_2(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_registries(mock_api, {"2091": 1000})
    mock_api.post(f"{BFF}/openbanking/aggregations/data-actualizations").mock(return_value=httpx.Response(202))
    mock_api.get(AGGS).mock(return_value=httpx.Response(200, json=[agg("2091", "IN_PROGRESS")]))

    result = runner.invoke(cli.app, ["refresh", "--timeout", "10"])

    assert result.exit_code == 2, result.output
    assert "Siguen sincronizando" in result.stderr


def test_cli_refresh_no_wait_skips_baseline(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    trigger = mock_api.post(f"{BFF}/openbanking/aggregations/data-actualizations").mock(
        return_value=httpx.Response(202)
    )
    result = runner.invoke(cli.app, ["refresh", "--no-wait"])
    assert result.exit_code == 0, result.output
    assert trigger.called


def test_cli_bank_2fa_sends_code_and_waits(
    mock_api: respx.MockRouter, store: MemorySessionStore, sleeps: list[float]
) -> None:
    mock_api.get(AGGS).mock(
        side_effect=[
            httpx.Response(200, json=[sms_2fa("2091")]),  # lookup
            httpx.Response(200, json=[sms_2fa("2091")]),  # not processed yet
            httpx.Response(200, json=[agg("2091")]),
        ]
    )
    put = mock_api.put(f"{BFF}/openbanking/aggregations/agg-2091/2fa").mock(return_value=httpx.Response(200))
    result = runner.invoke(cli.app, ["bank-2fa", "2091", "123456"])

    assert result.exit_code == 0, result.output
    assert _body(put) == {"otp": "123456"}
    assert len(sleeps) == 1
    assert "actualizados" in result.stderr


def test_cli_bank_2fa_error_exits_3(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(AGGS).mock(
        side_effect=[
            httpx.Response(200, json=[sms_2fa("2091")]),  # lookup
            httpx.Response(200, json=[agg("2091", "IN_PROGRESS")]),
            httpx.Response(200, json=[agg("2091", "ACTIVE_ERROR")]),
        ]
    )
    mock_api.put(f"{BFF}/openbanking/aggregations/agg-2091/2fa").mock(return_value=httpx.Response(200))

    result = runner.invoke(cli.app, ["bank-2fa", "2091", "123456"])

    assert result.exit_code == 3, result.output
    assert "Con error: 2091" in result.stderr


def test_cli_bank_2fa_rejects_bank_not_waiting(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(AGGS).mock(return_value=httpx.Response(200, json=[agg("2091")]))
    put = mock_api.put(f"{BFF}/openbanking/aggregations/agg-2091/2fa")
    result = runner.invoke(cli.app, ["bank-2fa", "2091", "123456"])
    assert isinstance(result.exception, InputError)
    assert not put.called


def test_cli_bank_2fa_needs_code_when_headless(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    mock_api.get(AGGS).mock(return_value=httpx.Response(200, json=[sms_2fa("2091")]))
    put = mock_api.put(f"{BFF}/openbanking/aggregations/agg-2091/2fa")
    result = runner.invoke(cli.app, ["bank-2fa", "2091"])
    assert isinstance(result.exception, InputError)
    assert not put.called


def test_cli_bank_2fa_confirms_push(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    push = agg("2091", "IN_PROGRESS", "2FA", "BACKGROUND_WAITING_PARAM")
    mock_api.get(AGGS).mock(return_value=httpx.Response(200, json=[push]))
    put = mock_api.put(f"{BFF}/openbanking/aggregations/agg-2091/2fa").mock(return_value=httpx.Response(200))
    result = runner.invoke(cli.app, ["bank-2fa", "2091", "--no-wait"])
    assert result.exit_code == 0, result.output
    assert _body(put) == {}
