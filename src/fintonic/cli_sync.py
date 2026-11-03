"""Terminal side of bank sync: asking for the bank's 2FA and reporting the outcome."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime

import typer
from rich.console import Console
from rich.status import Status

from fintonic.aggregation import AggregationStatus, SecondFactorRequest
from fintonic.client import FintonicClient, SecondFactorHandler


def ask_second_factor(console: Console, request: SecondFactorRequest) -> str | None:
    """Prompt for the code (or a push approval). Returns None when the user skips that bank."""
    again = " otra vez (la respuesta anterior no valió o caducó)" if request.retry else ""
    console.print(f"[yellow]El banco {request.bank_id} pide verificación en dos pasos{again}.[/yellow]")
    if request.message:
        console.print(request.message, markup=False, highlight=False)
    if request.push:
        approved = typer.confirm("¿Lo has aprobado ya en la app del banco?", default=True, err=True)
        return "" if approved else None
    label = request.placeholder or "Código"
    code: str = typer.prompt(f"{label} (vacío para saltar)", default="", show_default=False, err=True)
    return code.strip() or None


def _pausing(spinner: Status, console: Console) -> SecondFactorHandler:
    def handler(request: SecondFactorRequest) -> str | None:
        spinner.stop()
        try:
            return ask_second_factor(console, request)
        finally:
            spinner.start()

    return handler


def wait_and_report(
    client: FintonicClient,
    console: Console,
    *,
    timeout: int,
    bank_ids: Collection[str] | None = None,
    interactive: bool = False,
    answered: dict[str, object] | None = None,
    since: Mapping[str, int | None] | None = None,
) -> None:
    with console.status("Sincronizando bancos…") as spinner:
        status = client.wait_for_banks(
            timeout=timeout,
            bank_ids=bank_ids,
            on_2fa=_pausing(spinner, console) if interactive else None,
            answered=answered,
            since=since,
        )
    report(console, status, timeout, since)


EXIT_TIMEOUT = 2  # some bank was still syncing when the wait ran out
EXIT_NOT_SYNCED = 3  # some bank ended with an error, a pending or rejected 2FA, or no sign of syncing


def report(
    console: Console, status: AggregationStatus, timeout: int, since: Mapping[str, int | None] | None = None
) -> None:
    """Print each bank's outcome; exit non-zero unless every bank synced."""
    if status.needs_2fa:
        console.print(
            f"[yellow]Esperan 2FA: {', '.join(status.needs_2fa)}. Apruébalo en la app del banco y ejecuta "
            "`fintonic bank-2fa BANCO`, o envía el código con `fintonic bank-2fa BANCO CODIGO`.[/yellow]"
        )
    if status.invalid_2fa:
        console.print(
            f"[red]2FA rechazado o caducado: {', '.join(status.invalid_2fa)}. "
            "Vuelve a intentarlo con `fintonic refresh --bank BANCO`.[/red]"
        )
    if status.errors:
        console.print(f"[red]Con error: {', '.join(status.errors)}[/red]")
    if status.unconfirmed:
        console.print(
            f"[yellow]Sin constancia de que hayan sincronizado: {_with_last_sync(status.unconfirmed, since)}. "
            "Puede que Fintonic ignorase la petición (p.ej. por una sincronización reciente); "
            "mira `fintonic banks`.[/yellow]"
        )
    if status.in_progress:
        console.print(f"[yellow]Siguen sincronizando tras {timeout}s: {', '.join(status.in_progress)}[/yellow]")
        raise typer.Exit(EXIT_TIMEOUT)
    if status.failed:
        raise typer.Exit(EXIT_NOT_SYNCED)
    console.print("[green]Bancos actualizados.[/green]")


def _with_last_sync(banks: Collection[str], since: Mapping[str, int | None] | None) -> str:
    def one(bank: str) -> str:
        last = (since or {}).get(bank)
        return f"{bank} (última {datetime.fromtimestamp(last / 1000):%Y-%m-%d %H:%M})" if last else bank

    return ", ".join(one(b) for b in banks)
