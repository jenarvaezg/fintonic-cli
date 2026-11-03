"""`fintonic` command-line interface."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console

from fintonic import budgets as local_budgets
from fintonic.auth import Authenticator, stored_pin_relogin
from fintonic.cashflow import monthly_cash_flow, monthly_category_net
from fintonic.client import PRODUCT_ENDPOINTS, FintonicClient
from fintonic.errors import DeviceVerificationRequired, FintonicError, InputError, NotLoggedInError
from fintonic.http import build_http_client
from fintonic.models import Category, Transaction, cents_to_decimal
from fintonic.output import OutputFormat, format_eur, print_json, render
from fintonic.split import parse_part
from fintonic.stores import SessionStore, StoreKind, resolve_store
from fintonic.summary import Summary, month_range, summarize, summarize_nets
from fintonic.transfers import TRANSFER_CATEGORY, TransferPair, find_transfers, tx_date

# Never render locals in tracebacks: frames hold the PIN and tokens.
app = typer.Typer(no_args_is_help=True, help="CLI no oficial para Fintonic.", pretty_exceptions_show_locals=False)
err = Console(stderr=True)

FormatOpt = Annotated[OutputFormat, typer.Option("--format", "-f", help="Formato de salida.")]


@dataclass
class _StoreOptions:
    kind: StoreKind = StoreKind.auto
    path: Path | None = None


_store_options = _StoreOptions()


@app.callback()
def _global_options(
    store: Annotated[
        StoreKind,
        typer.Option(
            "--store",
            envvar="FINTONIC_STORE",
            help="Dónde guardar la sesión: keyring, file (0600) o auto (keyring si hay, si no file).",
        ),
    ] = StoreKind.auto,
    store_path: Annotated[
        Path | None,
        typer.Option(envvar="FINTONIC_STORE_PATH", help="Ruta del fichero con --store file."),
    ] = None,
) -> None:
    _store_options.kind = store
    _store_options.path = store_path


def _store() -> SessionStore:
    return resolve_store(_store_options.kind, _store_options.path)


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _client(store: SessionStore | None = None) -> FintonicClient:
    store = store or _store()
    session = store.load()
    if not session or not session.access_token:
        raise NotLoggedInError()
    if not session.device_verified:
        raise DeviceVerificationRequired()
    http = build_http_client()
    relogin = stored_pin_relogin(Authenticator(http), store)
    return FintonicClient(session, http=http, store=store, relogin=relogin)


def _local_time(moment: datetime | None) -> str:
    """Session expiry is UTC; show it in the user's timezone, naming the zone."""
    return moment.astimezone().strftime("%H:%M %Z") if moment else "?"


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise InputError(f"Fecha inválida {value!r}: usa YYYY-MM-DD.") from exc


def _read_pin(pin_stdin: bool) -> str:
    if pin_stdin:  # explicit flag wins over a possibly stale env var
        pin = sys.stdin.readline().strip()
        if not pin:
            raise typer.BadParameter("No se ha recibido el PIN por stdin.")
        return pin
    if pin := os.environ.get("FINTONIC_PIN"):
        return pin
    return typer.prompt("PIN", hide_input=True)


@app.command()
def login(
    email: Annotated[
        str | None,
        typer.Option(envvar="FINTONIC_EMAIL", help="Email de la cuenta (la API lo recibe como `username`)."),
    ] = None,
    remember: Annotated[bool, typer.Option(help="Guardar el PIN para re-login automático.")] = False,
    pin_stdin: Annotated[bool, typer.Option(help="Leer el PIN de stdin (también vale FINTONIC_PIN).")] = False,
    request_sms: Annotated[
        bool,
        typer.Option(help="No pedir el código: enviar el SMS y salir. Completar con `fintonic verify CODIGO`."),
    ] = False,
) -> None:
    """Inicia sesión. La primera vez en un dispositivo pide verificación por SMS."""
    store = _store()
    previous = store.load()
    email = email or (previous.email if previous else None) or typer.prompt("Email")
    pin = _read_pin(pin_stdin)

    with build_http_client() as http:
        auth = Authenticator(http)
        result = auth.login(email, pin, previous=previous)
        session = result.session
        if previous and previous.email != email:
            store.delete_pin(previous.email)
        if remember:  # the PIN was accepted; SMS only concerns the device
            store.save_pin(email, pin)
        if result.needs_device_verification:
            if result.device_changed:
                err.print("[yellow]Fintonic ha asignado un dispositivo nuevo: hay que verificarlo otra vez.[/yellow]")
            auth.request_verification_code(session)
            if request_sms or not _stdin_is_tty():
                store.save(session)
                err.print("SMS enviado. Completa con `fintonic verify CODIGO` antes de que caduque la sesión (30 min).")
                return
            session = auth.verify_device(session, typer.prompt("Código SMS"))

    store.save(session)
    err.print(f"[green]Sesión iniciada[/green] como {email} (caduca a las {_local_time(session.expires_at)}).")


@app.command()
def verify(code: Annotated[str, typer.Argument(help="Código recibido por SMS.")]) -> None:
    """Completa la verificación por SMS iniciada con `login --request-sms`."""
    store = _store()
    session = store.load()
    if not session or not session.access_token:
        raise NotLoggedInError()
    if session.device_verified:
        err.print("El dispositivo ya está verificado.")
        return
    with build_http_client() as http:
        session = Authenticator(http).verify_device(session, code)
    store.save(session)
    err.print(f"[green]Dispositivo verificado.[/green] Sesión activa hasta las {_local_time(session.expires_at)}.")


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
            "store": type(store).__name__,
            "logged_in": bool(session.access_token) and not session.is_expired(),
            "expires_at": expires.astimezone().isoformat() if expires else None,
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
            "id": p.id,
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


def _product_names(client: FintonicClient, txs: list[Transaction]) -> dict[str, str]:
    """Map transaction product_id -> product name, fetching only the product kinds present."""
    kinds = {"ACCOUNT": "account", "CREDITCARD": "credit_card"}
    needed = {kinds[t.type] for t in txs if t.type in kinds}
    return {key: p.name for p in client.all_products(sorted(needed)) for key in p.transaction_keys}


def _category_label(t: Transaction, cats: dict[str, Category]) -> str:
    cid = t.category_id
    name = cats[cid].name if cid in cats else (cid or "")
    return f"{name} +{len(t.category_amounts) - 1}" if t.is_split else name


@app.command()
def transactions(
    fmt: FormatOpt = OutputFormat.table,
    since: Annotated[str | None, typer.Option("--from", help="Fecha inicio YYYY-MM-DD.")] = None,
    until: Annotated[str | None, typer.Option("--to", help="Fecha fin YYYY-MM-DD.")] = None,
    category: Annotated[str | None, typer.Option("--category", "-c", help="Id de categoría (p.ej. G0898).")] = None,
    search: Annotated[str | None, typer.Option("--search", "-s", help="Texto a buscar.")] = None,
    bank: Annotated[str | None, typer.Option("--bank", help="bank_id (ver `fintonic banks`).")] = None,
    product: Annotated[str | None, typer.Option("--product", help="id de producto (ver `fintonic products`).")] = None,
    unread: Annotated[bool, typer.Option("--unread", help="Solo movimientos sin leer.")] = False,
    limit: Annotated[int | None, typer.Option("--limit", "-n", help="Máximo de movimientos (0 = todos).")] = 50,
) -> None:
    """Movimientos, del más reciente al más antiguo."""
    begin, end = _parse_date(since), _parse_date(until)  # validate input before any network call
    with _client() as client:
        cats = client.categories()
        txs = list(
            client.transactions(
                begin=begin,
                end=end,
                category_id=category,
                query=search,
                bank_id=bank,
                product=client.product(product) if product else None,
                unread=unread,
                limit=limit or None,
            )
        )
        products = _product_names(client, txs)

    if fmt is OutputFormat.json:
        print_json(
            [
                {
                    "id": t.id,
                    "user_date": t.user_date,
                    "value_date": t.value_date,
                    "operation_date": t.operation_date,
                    "concept": t.label,
                    "amount": t.amount,
                    "currency": t.currency,
                    "category_id": t.category_id,
                    "category": cats[t.category_id].name if t.category_id in cats else None,
                    "categories": {cid: str(cents_to_decimal(c)) for cid, c in t.category_amounts.items()},
                    "split": t.is_split,
                    "read": t.read,
                    "bank_id": t.bank_id,
                    "product_id": t.product_id,
                    "product": products.get(t.product_id),
                }
                for t in txs
            ]
        )
        return

    table = fmt is OutputFormat.table
    rows = [
        {
            "fecha": t.user_date or t.value_date,
            "f.operación": t.operation_date,
            "concepto": t.label,
            "categoría": _category_label(t, cats),
            "importe": format_eur(t.quantity) if table else t.amount,
            "producto": products.get(t.product_id) or t.bank_id,
            "leído": ("" if t.read else "●") if table else t.read,
            "id": t.id,
        }
        for t in txs
    ]
    render(rows, fmt, title=f"Movimientos ({len(rows)})")


@app.command()
def summary(
    fmt: FormatOpt = OutputFormat.table,
    month: Annotated[str | None, typer.Option("--month", "-m", help="Mes YYYY-MM (por defecto, el actual).")] = None,
    group: Annotated[
        str, typer.Option("--group", help="category (hoja) o parent (grupo, p.ej. 'Gastos varios').")
    ] = "category",
    exclude_bank: Annotated[list[str] | None, typer.Option("--exclude-bank", help="bank_id a excluir.")] = None,
    exclude_product: Annotated[
        list[str] | None, typer.Option("--exclude-product", help="id de producto a excluir.")
    ] = None,
    local: Annotated[
        bool, typer.Option("--local", help="Calcular paginando los movimientos en vez de usar las estadísticas.")
    ] = False,
    budgets: Annotated[bool, typer.Option("--budgets", help="Comparar con los presupuestos locales.")] = False,
    budgets_file: Annotated[
        Path | None,
        typer.Option(
            "--budgets-file",
            envvar=local_budgets.ENV_VAR,
            help="Fichero TOML de presupuestos (por defecto $XDG_CONFIG_HOME/fintonic/budgets.toml).",
        ),
    ] = None,
    fail_over_budget: Annotated[
        bool, typer.Option("--fail-over-budget", help="Salir con código 3 si algún presupuesto se ha superado.")
    ] = False,
) -> None:
    """Ingresos y gastos del mes por categoría (sin no computables: traspasos, inversiones...).

    Sin --exclude-* usa las estadísticas ya calculadas por Fintonic (una sola llamada).
    """
    if group not in {"category", "parent"}:
        raise typer.BadParameter("--group debe ser 'category' o 'parent'.")
    begin, end = month_range(month or date.today().strftime("%Y-%m"))
    use_budgets = budgets or fail_over_budget
    limits = local_budgets.load_budgets(budgets_file or local_budgets.default_path()) if use_budgets else {}
    with _client() as client:
        cats = client.categories()
        if limits:
            local_budgets.check_known(limits, cats)
        if local or exclude_bank or exclude_product:
            excluded_keys = frozenset(k for pid in exclude_product or [] for k in client.product(pid).transaction_keys)
            excluded_banks = frozenset(exclude_bank or [])
            txs = [
                t
                for t in client.transactions(begin=begin, end=end)
                if t.bank_id not in excluded_banks and t.product_id not in excluded_keys
            ]
            leaf = summarize(txs, cats)
            result = summarize(txs, cats, group_by_parent=True) if group == "parent" else leaf
        else:
            nets = client.category_nets(begin, end)
            leaf = summarize_nets(nets, cats)
            result = summarize_nets(nets, cats, group_by_parent=True) if group == "parent" else leaf
    statuses = local_budgets.evaluate(limits, leaf.rows, cats) if limits else []
    _print_summary(result, statuses, begin, fmt, with_budgets=use_budgets)
    if fail_over_budget and any(s.over for s in statuses):
        raise typer.Exit(3)


def _budget_rows(statuses: list[local_budgets.BudgetStatus], fmt: OutputFormat) -> list[dict[str, Any]]:
    table = fmt is OutputFormat.table
    money = format_eur if table else cents_to_decimal
    return [
        {
            "id": s.category_id,
            "categoría": s.name,
            "presupuesto": money(s.limit),
            "gastado": money(s.spent),
            "%": f"{s.percent} %" if table else s.percent,
            "excedido": ("SÍ" if s.over else "") if table else s.over,
        }
        for s in statuses
    ]


def _print_summary(
    result: Summary, statuses: list[local_budgets.BudgetStatus], begin: date, fmt: OutputFormat, *, with_budgets: bool
) -> None:
    if fmt is OutputFormat.json:
        body: dict[str, Any] = {
            "month": begin.strftime("%Y-%m"),
            "income": cents_to_decimal(result.income),
            "expenses": cents_to_decimal(result.expenses),
            "net": cents_to_decimal(result.net),
            "excluded_non_computable": cents_to_decimal(result.excluded_non_computable),
            "categories": [
                {
                    "id": r.category_id,
                    "name": r.name,
                    "kind": r.kind,
                    "amount": cents_to_decimal(r.amount),
                    "count": r.count,
                }
                for r in result.rows
            ],
        }
        if with_budgets:
            body["budgets"] = [
                {
                    "id": s.category_id,
                    "name": s.name,
                    "limit": cents_to_decimal(s.limit),
                    "spent": cents_to_decimal(s.spent),
                    "percent": s.percent,
                    "over": s.over,
                }
                for s in statuses
            ]
        print_json(body)
        return

    if fmt is OutputFormat.csv and with_budgets:  # one table per CSV: budgets were asked for
        render(_budget_rows(statuses, fmt), fmt)
        return

    table = fmt is OutputFormat.table
    money = format_eur if table else cents_to_decimal
    rows = [
        {"tipo": r.kind, "id": r.category_id, "categoría": r.name, "movs": r.count, "importe": money(r.amount)}
        for r in result.rows
    ]
    render(rows, fmt, title=f"Resumen {begin:%Y-%m}")
    if not table:
        return
    err.print(
        f"Ingresos {format_eur(result.income)} · Gastos {format_eur(result.expenses)} · "
        f"[bold]Neto {format_eur(result.net)}[/bold]  (no computable excluido: "
        f"{format_eur(result.excluded_non_computable)})"
    )
    if with_budgets:
        render(_budget_rows(statuses, fmt), fmt, title=f"Presupuestos {begin:%Y-%m}")
        over = [s.category_id for s in statuses if s.over]
        if over:
            err.print(f"[red]Presupuesto superado en: {', '.join(over)}[/red]")


@app.command()
def cashflow(
    fmt: FormatOpt = OutputFormat.table,
    months: Annotated[int, typer.Option("--months", "-n", min=1, help="Número de meses.")] = 12,
    until: Annotated[
        str | None, typer.Option("--to", help="Último mes YYYY-MM incluido (por defecto, el actual).")
    ] = None,
    category: Annotated[
        str | None, typer.Option("--category", "-c", help="Solo esta categoría o grupo (p.ej. G0898, G0800).")
    ] = None,
) -> None:
    """Ingresos, gastos y neto por mes (estadísticas de Fintonic, sin no computables)."""
    pivot = month_range(until or date.today().strftime("%Y-%m"))[0]
    table = fmt is OutputFormat.table
    money = format_eur if table else cents_to_decimal
    with _client() as client:
        if category:
            category_id = category.upper()
            cats = client.categories()
            series = monthly_category_net(client, category_id, cats, pivot, months)
            name = cats[category_id].name
            rows = [{"mes": f"{m.start:%Y-%m}", "movs": m.count, "importe": money(m.net)} for m in series]
            render(rows, fmt, title=f"{name} ({category_id}) por mes")
            return
        flows = monthly_cash_flow(client, pivot, months)
    rows = [
        {"mes": f"{f.start:%Y-%m}", "ingresos": money(f.income), "gastos": money(f.expenses), "neto": money(f.net)}
        for f in flows
    ]
    render(rows, fmt, title="Flujo de caja")
    if table and flows:
        err.print(
            f"Total: ingresos {format_eur(sum(f.income for f in flows))} · "
            f"gastos {format_eur(sum(f.expenses for f in flows))} · "
            f"[bold]neto {format_eur(sum(f.net for f in flows))}[/bold]"
        )


def _period(month: str | None, since: str | None, until: str | None) -> tuple[date, date]:
    if month and (since or until):
        raise typer.BadParameter("Usa --month o --from/--to, no ambos.")
    if since or until:
        begin, end = _parse_date(since), _parse_date(until)
        today = date.today()
        begin = begin or (end or today).replace(day=1)
        end = end or today
        if begin > end:
            raise typer.BadParameter("--from es posterior a --to.")
        return begin, end
    return month_range(month or date.today().strftime("%Y-%m"))


def _cat_label(t: Transaction, cats: dict[str, Category]) -> str:
    cid = t.category_id or ""
    return f"{cats[cid].name} ({cid})" if cid in cats else cid


@app.command()
def transfers(
    fmt: FormatOpt = OutputFormat.table,
    month: Annotated[str | None, typer.Option("--month", "-m", help="Mes YYYY-MM (por defecto, el actual).")] = None,
    since: Annotated[str | None, typer.Option("--from", help="Fecha inicio YYYY-MM-DD.")] = None,
    until: Annotated[str | None, typer.Option("--to", help="Fecha fin YYYY-MM-DD.")] = None,
    days: Annotated[int, typer.Option("--days", min=0, help="Máximo de días entre cargo y abono.")] = 3,
    apply: Annotated[
        bool, typer.Option("--apply", help=f"Recategorizar como {TRANSFER_CATEGORY} (escribe en tu cuenta).")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="No pedir confirmación con --apply.")] = False,
) -> None:
    """Detecta traspasos entre tus productos que no están en una categoría no computable (N*).

    Empareja un cargo y un abono del mismo importe en productos distintos con ±--days días.
    Sin --apply solo los lista.
    """
    begin, end = _period(month, since, until)
    margin = timedelta(days=days)
    with _client() as client:
        cats = client.categories()
        txs = list(client.transactions(begin=begin - margin, end=end + margin))
        pairs = [
            p
            for p in find_transfers(txs, days=days)
            if begin <= tx_date(p.debit) <= end or begin <= tx_date(p.credit) <= end
        ]
        products = _product_names(client, [t for p in pairs for t in (p.debit, p.credit)])
        _render_transfers(pairs, cats, products, fmt)
        targets = [t for p in pairs for t in p.to_recategorize]
        if not apply or not targets:
            return
        if TRANSFER_CATEGORY not in cats:
            raise InputError(f"La categoría {TRANSFER_CATEGORY} no existe en tu árbol de categorías.")
        if not yes and not typer.confirm(
            f"¿Recategorizar {len(targets)} movimientos como {TRANSFER_CATEGORY} ({cats[TRANSFER_CATEGORY].name})?"
        ):
            err.print("Cancelado.")
            raise typer.Exit(1)
        for t in targets:
            client.set_category(t.id, TRANSFER_CATEGORY)
    err.print(f"[green]{len(targets)} movimientos → {TRANSFER_CATEGORY}[/green]")


def _render_transfers(
    pairs: list[TransferPair], cats: dict[str, Category], products: dict[str, str], fmt: OutputFormat
) -> None:
    if fmt is OutputFormat.json:
        print_json(
            [
                {
                    "amount": cents_to_decimal(p.amount),
                    "days_apart": p.days_apart,
                    "to_recategorize": [t.id for t in p.to_recategorize],
                    **{
                        side: {
                            "id": t.id,
                            "date": tx_date(t),
                            "concept": t.label,
                            "category_id": t.category_id,
                            "product_id": t.product_id,
                            "product": products.get(t.product_id),
                        }
                        for side, t in (("debit", p.debit), ("credit", p.credit))
                    },
                }
                for p in pairs
            ]
        )
        return
    table = fmt is OutputFormat.table
    fix = {t.id for p in pairs for t in p.to_recategorize}

    def side(t: Transaction) -> str:
        return f"{_cat_label(t, cats)}{' → ' + TRANSFER_CATEGORY if t.id in fix else ''}"

    rows = [
        {
            "importe": format_eur(p.amount) if table else cents_to_decimal(p.amount),
            "fecha cargo": tx_date(p.debit),
            "origen": products.get(p.debit.product_id) or p.debit.bank_id,
            "concepto cargo": p.debit.label,
            "categoría cargo": side(p.debit),
            "fecha abono": tx_date(p.credit),
            "destino": products.get(p.credit.product_id) or p.credit.bank_id,
            "concepto abono": p.credit.label,
            "categoría abono": side(p.credit),
            "id cargo": p.debit.id,
            "id abono": p.credit.id,
        }
        for p in pairs
    ]
    render(rows, fmt, title=f"Posibles traspasos ({len(rows)})")


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


tx_app = typer.Typer(no_args_is_help=True, help="Editar movimientos (escribe en tu cuenta de Fintonic).")
app.add_typer(tx_app, name="tx")


@tx_app.command("show")
def tx_show(
    tx_id: Annotated[str, typer.Argument(help="id del movimiento.")],
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Detalle completo de un movimiento: fechas, producto, categorías y partes si está dividido."""
    with _client() as client:
        t = client.transaction(tx_id)
        cats = client.categories()
        product = _product_names(client, [t]).get(t.product_id)

    def cat_name(cid: str) -> str:
        return cats[cid].name if cid in cats else cid

    if fmt is OutputFormat.json:
        print_json(
            {
                **t.model_dump(by_alias=True, mode="json"),
                "amount": t.amount,
                "product": product,
                "category": cat_name(t.category_id) if t.category_id else None,
                "parts": [
                    {"category_id": cid, "category": cat_name(cid), "amount": cents_to_decimal(c)}
                    for cid, c in t.category_amounts.items()
                ],
            }
        )
        return

    money = format_eur if fmt is OutputFormat.table else cents_to_decimal
    parts = "; ".join(f"{cat_name(cid)} ({cid}) {money(c)}" for cid, c in t.category_amounts.items())
    fields = {
        "id": t.id,
        "concepto": t.label,
        "descripción banco": t.description,
        "nota": t.secondary_display or t.note or "",
        "importe": money(t.quantity),
        "fecha": t.user_date,
        "fecha valor": t.value_date,
        "fecha operación": t.operation_date,
        "categoría": f"{cat_name(t.category_id)} ({t.category_id})" if t.category_id else "",
        "dividido": f"sí: {parts}" if t.is_split else "no",
        "producto": product or t.product_id,
        "banco": t.bank_id,
        "tipo": t.type,
        "leído": "sí" if t.read else "no",
    }
    render([{"campo": k, "valor": v} for k, v in fields.items()], fmt, title=f"Movimiento {t.id}")


@tx_app.command("set-category")
def tx_set_category(
    tx_id: Annotated[str, typer.Argument(help="id del movimiento.")],
    category_id: Annotated[str, typer.Argument(help="Categoría destino (ver `fintonic categories`).")],
    similar: Annotated[
        bool, typer.Option("--similar", help="Aplicar también a movimientos similares (crea regla).")
    ] = False,
) -> None:
    """Recategoriza un movimiento."""
    with _client() as client:
        if category_id.upper() not in client.categories():
            raise typer.BadParameter(f"La categoría {category_id} no existe.")
        client.set_category(tx_id, category_id.upper(), apply_to_similar=similar)
    err.print(f"[green]Movimiento {tx_id} → {category_id.upper()}[/green]")


@tx_app.command("split")
def tx_split(
    tx_id: Annotated[str, typer.Argument(help="id del movimiento.")],
    parts: Annotated[
        list[str], typer.Argument(help="CATEGORIA:IMPORTE en euros positivos, p.ej. G0898:85 G0101:19,50")
    ],
) -> None:
    """Divide un movimiento en 2-3 categorías; las partes deben sumar el total."""
    parsed = [parse_part(p) for p in parts]
    with _client() as client:
        known = client.categories()
        unknown = [cid for cid, _ in parsed if cid not in known]
        if unknown:
            raise typer.BadParameter(f"Categorías inexistentes: {', '.join(unknown)}")
        client.split(client.transaction(tx_id), parsed)
    err.print(f"[green]Movimiento {tx_id} dividido en {len(parsed)} partes.[/green]")


@tx_app.command("describe")
def tx_describe(tx_id: str, text: str) -> None:
    """Cambia el concepto que se muestra."""
    with _client() as client:
        client.set_description(tx_id, text)


@tx_app.command("note")
def tx_note(tx_id: str, text: str) -> None:
    """Añade o cambia la nota del movimiento."""
    with _client() as client:
        client.set_note(tx_id, text)


@tx_app.command("mark-read")
def tx_mark_read(
    tx_ids: Annotated[list[str] | None, typer.Argument(help="ids a marcar como leídos.")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Marcar todos los movimientos.")] = False,
    bank: Annotated[str | None, typer.Option("--bank", help="Marcar todos los de este banco.")] = None,
) -> None:
    """Marca como leídos unos movimientos, todos los de un banco (--bank) o todos (--all)."""
    if tx_ids and (all_ or bank):
        raise typer.BadParameter("Usa ids, --bank o --all, pero no ids junto con --bank/--all.")
    if not tx_ids and not all_ and not bank:
        raise typer.BadParameter("Indica ids, --bank o --all.")
    with _client() as client:
        if tx_ids:
            for tx_id in tx_ids:
                client.mark_read(tx_id)
        else:
            client.mark_all_read(bank)
    err.print("[green]Hecho.[/green]")


@app.command()
def refresh(
    wait: Annotated[bool, typer.Option(help="Esperar a que terminen de sincronizar.")] = True,
    timeout: Annotated[int, typer.Option(help="Segundos máximos de espera.")] = 300,
) -> None:
    """Pide a todos los bancos que sincronicen (Perfil > Entidades > Actualizar)."""
    with _client() as client:
        client.refresh_banks()
        if not wait:
            err.print("Actualización solicitada.")
            return
        with err.status("Sincronizando bancos…"):
            status = client.wait_for_banks(timeout=timeout)
    if status.needs_2fa:
        err.print(f"[yellow]Requieren aprobar 2FA en la app del banco: {', '.join(status.needs_2fa)}[/yellow]")
    if status.errors:
        err.print(f"[red]Con error: {', '.join(status.errors)}[/red]")
    if status.in_progress:
        err.print(f"[yellow]Siguen sincronizando tras {timeout}s: {', '.join(status.in_progress)}[/yellow]")
        raise typer.Exit(2)
    if not status.errors:
        err.print("[green]Bancos actualizados.[/green]")


def main() -> None:
    try:
        app()
    except FintonicError as exc:
        err.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1) from exc
    except httpx.HTTPError as exc:
        err.print(f"[red]Error de red:[/red] {type(exc).__name__}. Fintonic no responde; reintenta en un rato.")
        raise SystemExit(1) from exc
