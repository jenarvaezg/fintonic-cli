"""`fintonic` command-line interface."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

import typer
from rich.console import Console

from fintonic.auth import Authenticator, stored_pin_relogin
from fintonic.client import PRODUCT_ENDPOINTS, FintonicClient
from fintonic.errors import FintonicError, NotLoggedInError
from fintonic.http import build_http_client
from fintonic.output import OutputFormat, format_eur, print_json, render
from fintonic.session import KeyringSessionStore, SessionStore

app = typer.Typer(no_args_is_help=True, help="CLI no oficial para Fintonic.")
err = Console(stderr=True)

FormatOpt = Annotated[OutputFormat, typer.Option("--format", "-f", help="Formato de salida.")]


def _store() -> SessionStore:
    return KeyringSessionStore()


def _client(store: SessionStore | None = None) -> FintonicClient:
    store = store or _store()
    session = store.load()
    if not session or not session.access_token:
        raise NotLoggedInError()
    http = build_http_client()
    relogin = stored_pin_relogin(Authenticator(http), store)
    return FintonicClient(session, http=http, store=store, relogin=relogin)


def _parse_date(value: str | None) -> date | None:
    return datetime.strptime(value, "%Y-%m-%d").date() if value else None


@app.command()
def login(
    email: Annotated[str | None, typer.Option(envvar="FINTONIC_EMAIL", help="Email de la cuenta.")] = None,
    remember: Annotated[bool, typer.Option(help="Guardar el PIN en el Keychain para re-login automático.")] = False,
) -> None:
    """Inicia sesión (PIN + SMS la primera vez en este dispositivo)."""
    store = _store()
    previous = store.load()
    email = email or (previous.email if previous else None) or typer.prompt("Email")
    pin = typer.prompt("PIN", hide_input=True)

    with build_http_client() as http:
        auth = Authenticator(http)
        result = auth.login(email, pin, previous=previous)
        session = result.session
        if result.needs_device_verification:
            auth.request_verification_code(session)
            code = typer.prompt("Código SMS")
            session = auth.verify_device(session, code)

    store.save(session)
    if remember:
        store.save_pin(email, pin)
    err.print(f"[green]Sesión iniciada[/green] como {email} (caduca {session.expires_at:%H:%M}).")


@app.command()
def logout(
    forget_device: Annotated[bool, typer.Option(help="Olvidar también el dispositivo (pedirá SMS otra vez).")] = False,
) -> None:
    """Cierra la sesión local y borra el PIN guardado."""
    store = _store()
    session = store.load()
    if session:
        store.delete_pin(session.email)
        if forget_device:
            store.clear()
        else:
            store.save(session.without_tokens())
    err.print("Sesión cerrada.")


@app.command()
def status() -> None:
    """Muestra el estado de la sesión local."""
    store = _store()
    session = store.load()
    if not session:
        err.print("Sin sesión.")
        raise typer.Exit(1)
    expires = session.expires_at
    print_json(
        {
            "email": session.email,
            "logged_in": bool(session.access_token) and not session.is_expired(),
            "expires_at": expires.isoformat() if expires else None,
            "device_verified": session.device_verified,
            "auto_relogin": store.load_pin(session.email) is not None,
        }
    )


@app.command()
def position(fmt: FormatOpt = OutputFormat.table, history: bool = False) -> None:
    """Saldo global por tipo de producto (y su histórico mensual con --history)."""
    with _client() as client:
        pos = client.position()
    entries = pos.history() if history else [("actual", pos.position)]
    rows = [
        {
            "mes": month,
            "cuentas": format_eur(b.account_balance) if fmt is OutputFormat.table else b.account_balance,
            "tarjetas": format_eur(b.credit_card_balance) if fmt is OutputFormat.table else b.credit_card_balance,
            "depósitos": format_eur(b.deposit_balance) if fmt is OutputFormat.table else b.deposit_balance,
            "fondos": format_eur(b.fund_balance) if fmt is OutputFormat.table else b.fund_balance,
            "acciones": format_eur(b.share_balance) if fmt is OutputFormat.table else b.share_balance,
            "pensiones": format_eur(b.pension_plan_balance) if fmt is OutputFormat.table else b.pension_plan_balance,
            "préstamos": format_eur(b.loan_balance) if fmt is OutputFormat.table else b.loan_balance,
            "total": format_eur(b.net_worth) if fmt is OutputFormat.table else b.net_worth,
        }
        for month, b in entries
    ]
    render(rows, fmt, title="Posición")


@app.command()
def banks(fmt: FormatOpt = OutputFormat.table) -> None:
    """Bancos conectados y estado de sincronización de sus productos."""
    with _client() as client:
        registries = client.banks()
    rows = [
        {
            "bank_id": b.bank_id,
            "nombre": b.name,
            "activo": b.active,
            "productos": ", ".join(f"{p.type}{'' if p.ok else ' (error)'}" for p in b.products),
            "última_sync": datetime.fromtimestamp(b.last_update_date / 1000).isoformat(timespec="minutes")
            if b.last_update_date
            else None,
        }
        for b in registries
    ]
    render(rows, fmt, title="Bancos")


@app.command()
def products(
    kind: Annotated[str | None, typer.Argument(help=f"Uno de: {', '.join(PRODUCT_ENDPOINTS)}. Vacío = todos.")] = None,
    fmt: FormatOpt = OutputFormat.table,
    all_: Annotated[bool, typer.Option("--all", help="Incluir productos cancelados o inactivos.")] = False,
) -> None:
    """Cuentas, tarjetas, préstamos, fondos... con su saldo."""
    with _client() as client:
        items = client.products(kind) if kind else client.all_products()
    items = [p for p in items if all_ or p.is_live]
    if fmt is OutputFormat.json:
        print_json([p.model_dump(by_alias=True) for p in items])
        return
    table = fmt is OutputFormat.table
    rows = [
        {
            "tipo": p.kind,
            "banco": p.bank_id,
            "nombre": p.name,
            "número": p.masked_number,
            "saldo": format_eur(p.balance) if table else p.balance,
            **({} if p.is_live else {"estado": "cancelado"}),
        }
        for p in items
    ]
    render(rows, fmt, title="Productos")


@app.command()
def categories(fmt: FormatOpt = OutputFormat.table, all_: Annotated[bool, typer.Option("--all")] = False) -> None:
    """Árbol de categorías (por defecto solo las clasificables)."""
    with _client() as client:
        cats = client.categories()
    rows = [
        {"id": c.id, "emoji": c.emoji or "", "nombre": c.name, "padre": c.ancestors[-1] if c.ancestors else ""}
        for c in sorted(cats.values(), key=lambda c: c.id)
        if all_ or c.classified
    ]
    render(rows, fmt, title="Categorías")


@app.command()
def transactions(
    fmt: FormatOpt = OutputFormat.table,
    since: Annotated[str | None, typer.Option("--from", help="Fecha inicio YYYY-MM-DD.")] = None,
    until: Annotated[str | None, typer.Option("--to", help="Fecha fin YYYY-MM-DD.")] = None,
    category: Annotated[str | None, typer.Option("--category", "-c", help="Id de categoría (p.ej. G0898).")] = None,
    search: Annotated[str | None, typer.Option("--search", "-s", help="Texto a buscar.")] = None,
    bank: Annotated[str | None, typer.Option("--bank", help="bank_id (ver `fintonic banks`).")] = None,
    limit: Annotated[int | None, typer.Option("--limit", "-n", help="Máximo de movimientos.")] = 50,
) -> None:
    """Movimientos, del más reciente al más antiguo. Usa --limit 0 para todos."""
    with _client() as client:
        cats = client.categories()
        txs = list(
            client.transactions(
                begin=_parse_date(since),
                end=_parse_date(until),
                category_id=category,
                query=search,
                bank_id=bank,
                limit=limit or None,
            )
        )
    table = fmt is OutputFormat.table
    rows = [
        {
            "fecha": t.user_date or t.value_date,
            "concepto": t.label,
            "categoría": cats[t.category_id].name if t.category_id in cats else t.category_id,
            "importe": format_eur(t.quantity) if table else t.amount,
            "banco": t.bank_id,
            **({} if table else {"id": t.id, "category_id": t.category_id, "currency": t.currency}),
        }
        for t in txs
    ]
    render(rows, fmt, title=f"Movimientos ({len(rows)})")


@app.command()
def raw(
    path: Annotated[str, typer.Argument(help="Ruta relativa, p.ej. rest/user/profile")],
    bff: Annotated[bool, typer.Option(help="Usar el BFF (fintonic-pfm-bff) en vez de finapi.")] = False,
    param: Annotated[list[str] | None, typer.Option("--param", "-p", help="key=value, repetible.")] = None,
) -> None:
    """GET a cualquier endpoint y vuelca el JSON (para explorar la API)."""
    params = dict(p.split("=", 1) for p in param or [])
    with _client() as client:
        print_json(client.get(path, params, bff=bff))


def main() -> None:
    try:
        app()
    except FintonicError as exc:
        err.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1) from exc
