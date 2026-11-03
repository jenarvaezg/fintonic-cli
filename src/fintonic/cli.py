"""`fintonic` command-line interface."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.markup import escape

from fintonic import budgets as local_budgets
from fintonic.aggregation import SecondFactorRequest, is_invalid_2fa, needs_2fa
from fintonic.amounts import amount_bounds
from fintonic.auth import Authenticator, stored_pin_relogin
from fintonic.cashflow import monthly_cash_flow, monthly_category_net
from fintonic.changelog import ChangeLog, ChangeRecord
from fintonic.changes import (
    LABELS,
    Field,
    current_value,
    describe_value,
    single_category,
    split_categories,
    write_value,
)
from fintonic.cli_group import DefaultCommandGroup
from fintonic.cli_sync import ask_second_factor, wait_and_report
from fintonic.client import PRODUCT_ENDPOINTS, TRANSACTION_PRODUCT_KINDS, FintonicClient, product_read_path
from fintonic.errors import (
    DeviceVerificationRequired,
    FintonicError,
    InputError,
    NotLoggedInError,
    SessionExpiredError,
)
from fintonic.http import build_http_client
from fintonic.models import Category, Product, Transaction, cents_to_decimal
from fintonic.output import OutputFormat, format_eur, print_json, render
from fintonic.session import Session
from fintonic.split import parse_part
from fintonic.stores import SessionStore, StoreKind, resolve_store
from fintonic.summary import Summary, month_range, summarize, summarize_nets
from fintonic.transfers import TRANSFER_CATEGORY, TransferPair, find_transfers, tx_date
from fintonic.undo import no_history, plan_undo

# Never render locals in tracebacks: frames hold the PIN and tokens.
app = typer.Typer(no_args_is_help=True, help="CLI no oficial para Fintonic.", pretty_exceptions_show_locals=False)
err = Console(stderr=True)

# Logout on the server is best-effort; don't keep the user waiting a full request timeout.
LOGOUT_TIMEOUT = 15.0

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
    return _parse_day(value) if value else None


def _parse_day(value: str) -> date:
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
    """Cierra la sesión en Fintonic y en local, y borra el PIN guardado."""
    store = _store()
    session = store.load()
    if session:
        # Local state goes first so it is cleared even if the server call hangs or fails.
        store.delete_pin(session.email)
        if forget_device:
            store.clear()
        else:
            store.save(session.without_tokens())
        _server_logout(session)
    err.print("Sesión cerrada.")


def _server_logout(session: Session) -> None:
    """Best-effort `/oidc/logout`: failures only warn, the local session is already gone."""
    if not session.refresh_token:
        if session.access_token:
            err.print(
                "[yellow]Esta sesión se guardó con una versión anterior y no tiene refresh_token: solo se cierra "
                "en local (el token de Fintonic caducará solo en 30 min como mucho).[/yellow]"
            )
        return
    try:
        with build_http_client(timeout=LOGOUT_TIMEOUT) as http:
            Authenticator(http).logout(session)
    except SessionExpiredError:
        err.print("[yellow]Fintonic ha rechazado el cierre de sesión (token caducado o ya invalidado).[/yellow]")
    except FintonicError as exc:
        _warn_server_logout(escape(str(exc)))
    except httpx.HTTPError as exc:
        _warn_server_logout(f"error de red: {type(exc).__name__}")


def _warn_server_logout(reason: str) -> None:
    err.print(f"[yellow]No se ha podido cerrar la sesión en Fintonic ({reason}); la local sí se ha borrado.[/yellow]")


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


products_app = typer.Typer(
    cls=DefaultCommandGroup,
    help="Cuentas, tarjetas, préstamos, fondos... (`products [TIPO]` lista; `hide`/`show` los ocultan o muestran).",
)
app.add_typer(products_app, name="products")


def _product_state(p: Product) -> str:
    return "cancelado" if p.canceled else "oculto"


@products_app.command("list")
def products(
    kind: Annotated[str | None, typer.Argument(help=f"Uno de: {', '.join(PRODUCT_ENDPOINTS)}. Vacío = todos.")] = None,
    fmt: FormatOpt = OutputFormat.table,
    all_: Annotated[bool, typer.Option("--all", help="Incluir productos cancelados u ocultos.")] = False,
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
            # Every row needs the column: tables and CSV take their columns from the first row.
            **({"estado": "" if p.is_live else _product_state(p)} if all_ else {}),
        }
        for p in items
    ]
    render(rows, fmt, title="Productos")


ProductIdsArg = Annotated[list[str] | None, typer.Argument(help="ids de producto (ver `fintonic products --all`).")]
BankOpt = Annotated[str | None, typer.Option("--bank", help="Todos los productos de este banco.")]
KindOpt = Annotated[str | None, typer.Option("--kind", help=f"Con --bank: solo {', '.join(PRODUCT_ENDPOINTS)}.")]


def _set_visibility(product_ids: list[str] | None, bank: str | None, kind: str | None, *, visible: bool) -> None:
    if product_ids and bank:
        raise typer.BadParameter("Usa ids o --bank, pero no ambos.")
    if not product_ids and not bank:
        raise typer.BadParameter("Indica ids de producto o --bank.")
    if kind and not bank:
        raise typer.BadParameter("--kind solo tiene sentido con --bank.")
    if kind and kind not in PRODUCT_ENDPOINTS:
        raise typer.BadParameter(f"Tipo desconocido: {kind}. Opciones: {', '.join(PRODUCT_ENDPOINTS)}.")
    with _client() as client:
        catalog = client.all_products()
        if bank:
            kinds = [kind] if kind else sorted({p.kind for p in catalog if p.bank_id == bank})
            if not kinds:
                raise InputError(f"El banco {bank} no tiene productos.")
            for k in kinds:
                client.set_bank_products_visible(bank, k, visible)
        else:
            by_id = {p.id: p for p in catalog}
            unknown = [pid for pid in product_ids or [] if pid not in by_id]
            if unknown:
                raise InputError(f"No existen los productos: {', '.join(unknown)}. Mira `fintonic products --all`.")
            for pid in product_ids or []:
                client.set_product_visible(by_id[pid], visible)
    err.print(f"[green]{'Visibles' if visible else 'Ocultos'}.[/green]")


@products_app.command("hide")
def products_hide(product_ids: ProductIdsArg = None, bank: BankOpt = None, kind: KindOpt = None) -> None:
    """Oculta productos, como desactivar una cuenta en la app (no borra nada)."""
    _set_visibility(product_ids, bank, kind, visible=False)


@products_app.command("show")
def products_show(product_ids: ProductIdsArg = None, bank: BankOpt = None, kind: KindOpt = None) -> None:
    """Vuelve a mostrar productos ocultos."""
    _set_visibility(product_ids, bank, kind, visible=True)


@app.command()
def mandates(
    mandate_id: Annotated[str | None, typer.Argument(help="id de un recibo para ver solo ese.")] = None,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Recibos domiciliados (mandatos de domiciliación) que Fintonic ha detectado."""
    with _client() as client:
        items = [client.mandate(mandate_id)] if mandate_id else client.mandates()
        cats = client.categories() if items else {}

    def cat_name(cid: str | None) -> str | None:
        return cats[cid].label if cid in cats else cid

    if fmt is OutputFormat.json:
        dumped = [
            {
                "mandate_id": m.mandate_id,
                "bank_id": m.bank_id,
                "business_name": m.business_name,
                "category_id": m.category_id,
                "category": cat_name(m.category_id),
                "status": m.status,
            }
            for m in items
        ]
        print_json(dumped[0] if mandate_id else dumped)
        return
    rows = [
        {
            "id": m.mandate_id,
            "banco": m.bank_id,
            "acreedor": m.business_name,
            "categoría": cat_name(m.category_id) or "",
            "estado": m.status,
        }
        for m in items
    ]
    render(rows, fmt, title="Recibos domiciliados")


@app.command()
def categories(fmt: FormatOpt = OutputFormat.table, all_: Annotated[bool, typer.Option("--all")] = False) -> None:
    """Árbol de categorías (por defecto solo las clasificables)."""
    with _client() as client:
        cats = client.categories()
    shown = [c for c in sorted(cats.values(), key=lambda c: c.id) if all_ or c.classified]
    if fmt is OutputFormat.json:
        print_json(
            [
                {
                    "id": c.id,
                    "emoji": c.emoji,
                    "name": c.label,
                    "description": c.name if c.name != c.label else None,
                    "parent_id": c.ancestors[-1] if c.ancestors else None,
                    "classified": c.classified,
                    "computable": c.computable,
                }
                for c in shown
            ]
        )
        return
    rows = [
        {"id": c.id, "emoji": c.emoji or "", "nombre": c.label, "padre": c.ancestors[-1] if c.ancestors else ""}
        for c in shown
    ]
    render(rows, fmt, title="Categorías")


def _tx_products(client: FintonicClient, txs: list[Transaction]) -> dict[str, Product]:
    """Map transaction product_id (ccc or card number) -> product, fetching only the product kinds present."""
    kinds = {"ACCOUNT": "account", "CREDITCARD": "credit_card"}
    needed = {kinds[t.type] for t in txs if t.type in kinds}
    return {key: p for p in client.all_products(sorted(needed)) for key in p.transaction_keys}


def _product_fields(t: Transaction, products: dict[str, Product]) -> dict[str, str | None]:
    """JSON fields naming the movement's product: `product_ref` is its id in `fintonic products` (and `--product`)."""
    product = products.get(t.product_id)
    return {
        "product_id": t.product_id,
        "product_ref": product.id if product else None,
        "product": product.name if product else None,
    }


def _product_name(products: dict[str, Product], t: Transaction) -> str:
    product = products.get(t.product_id)
    return (product.name if product else "") or t.bank_id


def _category_label(t: Transaction, cats: dict[str, Category]) -> str:
    cid = t.category_id
    name = cats[cid].label if cid in cats else (cid or "")
    return f"{name} +{len(t.category_amounts) - 1}" if t.is_split else name


AMOUNT_HELP = "Los importes llevan signo: los gastos son negativos (p.ej. --max -100 = gastos de 100 € o más)."


@app.command(epilog=AMOUNT_HELP)
def transactions(
    fmt: FormatOpt = OutputFormat.table,
    since: Annotated[str | None, typer.Option("--from", help="Fecha inicio YYYY-MM-DD.")] = None,
    until: Annotated[str | None, typer.Option("--to", help="Fecha fin YYYY-MM-DD.")] = None,
    category: Annotated[
        list[str] | None,
        typer.Option("--category", "-c", help="Id de categoría (p.ej. G0898). Repetible: vale cualquiera."),
    ] = None,
    search: Annotated[str | None, typer.Option("--search", "-s", help="Texto a buscar.")] = None,
    bank: Annotated[str | None, typer.Option("--bank", help="bank_id (ver `fintonic banks`).")] = None,
    product: Annotated[
        str | None, typer.Option("--product", help="id de cuenta o tarjeta (ver `fintonic products`).")
    ] = None,
    unread: Annotated[bool, typer.Option("--unread", help="Solo movimientos sin leer.")] = False,
    minimum: Annotated[
        str | None, typer.Option("--min", help="Importe mínimo en euros con signo (12,50 o -12.50).")
    ] = None,
    maximum: Annotated[str | None, typer.Option("--max", help="Importe máximo en euros con signo.")] = None,
    amount: Annotated[
        str | None,
        typer.Option("--amount", help="Importe exacto (-12,50) o rango X..Y (-100..-50, 100.., ..-20)."),
    ] = None,
    unclassified: Annotated[bool, typer.Option("--unclassified", help="Solo movimientos sin categorizar.")] = False,
    direct_debit: Annotated[bool, typer.Option("--direct-debit", help="Solo recibos domiciliados.")] = False,
    oldest_first: Annotated[bool, typer.Option("--oldest-first", help="Del más antiguo al más reciente.")] = False,
    limit: Annotated[int | None, typer.Option("--limit", "-n", help="Máximo de movimientos (0 = todos).")] = 50,
) -> None:
    """Movimientos, del más reciente al más antiguo (o al revés con --oldest-first). Filtra Fintonic."""
    # validate input before any network call
    begin, end = _parse_date(since), _parse_date(until)
    min_amount, max_amount = amount_bounds(minimum, maximum, amount)
    with _client() as client:
        cats = client.categories()
        selected = client.product(product) if product else None
        if selected and selected.kind not in TRANSACTION_PRODUCT_KINDS:
            err.print(f"[yellow]Solo cuentas y tarjetas tienen movimientos; este producto es {selected.kind}.[/yellow]")
        txs = list(
            client.transactions(
                begin=begin,
                end=end,
                category_ids=[c.upper() for c in category or []],
                query=search,
                bank_id=bank,
                product=selected,
                unread=unread,
                min_amount=min_amount,
                max_amount=max_amount,
                unclassified=unclassified,
                direct_debit=direct_debit,
                oldest_first=oldest_first,
                limit=limit or None,
            )
        )
        products = _tx_products(client, txs)

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
                    "category": cats[t.category_id].label if t.category_id in cats else None,
                    "categories": {cid: str(cents_to_decimal(c)) for cid, c in t.category_amounts.items()},
                    "split": t.is_split,
                    "read": t.read,
                    "bank_id": t.bank_id,
                    **_product_fields(t, products),
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
            "producto": _product_name(products, t),
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
            name = cats[category_id].label
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
    return f"{cats[cid].label} ({cid})" if cid in cats else cid


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
    max_changes: Annotated[
        int,
        typer.Option("--max-changes", min=1, help="Con --yes, no aplicar si hay más movimientos que recategorizar."),
    ] = 10,
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
        products = _tx_products(client, [t for p in pairs for t in (p.debit, p.credit)])
        _render_transfers(pairs, cats, products, fmt)
        targets = [t for p in pairs for t in p.to_recategorize]
        if not apply or not targets:
            return
        if TRANSFER_CATEGORY not in cats:
            raise InputError(f"La categoría {TRANSFER_CATEGORY} no existe en tu árbol de categorías.")
        if yes and len(targets) > max_changes:
            raise InputError(
                f"Hay {len(targets)} movimientos que recategorizar y --yes solo aplica hasta {max_changes} sin "
                "preguntar. Revisa la lista y confirma sin --yes, o sube --max-changes."
            )
        if not yes and not typer.confirm(
            f"¿Recategorizar {len(targets)} movimientos como {TRANSFER_CATEGORY} ({cats[TRANSFER_CATEGORY].label})?"
        ):
            err.print("Cancelado.")
            raise typer.Exit(1)
        for t in targets:  # through the change log, so `tx undo` can revert each side
            _change(
                t,
                Field.category,
                single_category(t, TRANSFER_CATEGORY),
                partial(client.set_category, t.id, TRANSFER_CATEGORY),
                command="transfers",
                dry_run=False,
                cats=cats,
            )
    err.print(f"[green]{len(targets)} movimientos → {TRANSFER_CATEGORY}[/green]")


def _render_transfers(
    pairs: list[TransferPair], cats: dict[str, Category], products: dict[str, Product], fmt: OutputFormat
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
                            **_product_fields(t, products),
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
            "origen": _product_name(products, p.debit),
            "concepto cargo": p.debit.label,
            "categoría cargo": side(p.debit),
            "fecha abono": tx_date(p.credit),
            "destino": _product_name(products, p.credit),
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
        products = _tx_products(client, [t])

    def cat_name(cid: str) -> str:
        return cats[cid].label if cid in cats else cid

    if fmt is OutputFormat.json:
        print_json(
            {
                **t.model_dump(by_alias=True, mode="json"),
                "amount": t.amount,
                **{k: v for k, v in _product_fields(t, products).items() if k != "product_id"},
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
        "producto": f"{products[t.product_id].name} ({products[t.product_id].id})"
        if t.product_id in products
        else t.product_id,
        "banco": t.bank_id,
        "tipo": t.type,
        "leído": "sí" if t.read else "no",
    }
    render([{"campo": k, "valor": v} for k, v in fields.items()], fmt, title=f"Movimiento {t.id}")


@tx_app.command("suggest")
def tx_suggest(
    tx_id: Annotated[str, typer.Argument(help="id del movimiento.")],
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Categorías que Fintonic propone para un movimiento, de más a menos probable (solo lectura)."""
    with _client() as client:
        suggestions = client.category_suggestions(tx_id)
        cats = client.categories() if suggestions else {}
    table = fmt is OutputFormat.table
    rows = [
        {
            "id": s.id,
            "categoría": cats[s.id].label if s.id in cats else "",
            "probabilidad": f"{s.probability:.0%}" if table else round(s.probability, 4),
        }
        for s in suggestions
    ]
    render(rows, fmt, title=f"Sugerencias para {tx_id}")


DryRunOpt = Annotated[bool, typer.Option("--dry-run", help="Muestra qué cambiaría sin escribir nada.")]


def _changelog() -> ChangeLog:
    return ChangeLog()


def _print_change(
    tx_id: str, field: Field, before: Any, after: Any, cats: Mapping[str, Category] | None, *, dry_run: bool
) -> None:
    prefix = "[yellow](simulación)[/yellow] " if dry_run else ""
    old, new = (escape(describe_value(field, v, cats)) for v in (before, after))
    err.print(f"{prefix}{escape(tx_id)} · {LABELS[field]}: {old} → {new}", soft_wrap=True)


def _record(tx: Transaction, field: Field, before: Any, after: Any, command: str, **extra: Any) -> None:
    """Log a write the API already accepted; a no-op is skipped so it never hides the change before it."""
    if before == after:
        return
    record = ChangeRecord(command=command, tx_id=tx.id, field=field, before=before, after=after, **extra)
    try:
        _changelog().append(record)
    except OSError as exc:
        err.print(
            f"[yellow]El cambio en {escape(tx.id)} se ha hecho, pero no se ha podido registrar en el historial "
            f"({type(exc).__name__}: {escape(str(exc))}); `tx undo` no lo verá.[/yellow]"
        )


def _change(
    tx: Transaction,
    field: Field,
    after: Any,
    write: Callable[[], None],
    *,
    command: str,
    dry_run: bool,
    cats: Mapping[str, Category] | None = None,
    **extra: Any,
) -> None:
    """Show `before → after`; unless dry-run, write it and log it once the API accepted it."""
    before = current_value(tx, field)
    _print_change(tx.id, field, before, after, cats, dry_run=dry_run)
    if dry_run:
        return
    write()
    _record(tx, field, before, after, command, **extra)


def _finish(dry_run: bool, message: str = "Hecho.") -> None:
    err.print("Simulación: no se ha escrito nada." if dry_run else f"[green]{message}[/green]")


@tx_app.command("set-category")
def tx_set_category(
    tx_id: Annotated[str, typer.Argument(help="id del movimiento.")],
    category_id: Annotated[str, typer.Argument(help="Categoría destino (ver `fintonic categories`).")],
    similar: Annotated[
        bool,
        typer.Option(
            "--similar",
            help="Aplicar también a movimientos similares (crea una regla que `tx undo` no puede deshacer).",
        ),
    ] = False,
    dry_run: DryRunOpt = False,
) -> None:
    """Recategoriza un movimiento."""
    category_id = category_id.upper()
    with _client() as client:
        cats = client.categories()
        if category_id not in cats:
            raise typer.BadParameter(f"La categoría {category_id} no existe.")
        tx = client.transaction(tx_id)
        write = partial(client.set_category, tx_id, category_id, apply_to_similar=similar)
        after = single_category(tx, category_id)
        _change(tx, Field.category, after, write, command="set-category", dry_run=dry_run, cats=cats, rule=similar)
    if similar:
        verb = "se crearía" if dry_run else "se ha creado"
        err.print(f"Además {verb} una regla para movimientos similares (no se puede deshacer con `tx undo`).")
    _finish(dry_run)


@tx_app.command("split")
def tx_split(
    tx_id: Annotated[str, typer.Argument(help="id del movimiento.")],
    parts: Annotated[
        list[str], typer.Argument(help="CATEGORIA:IMPORTE en euros positivos, p.ej. G0898:85 G0101:19,50")
    ],
    dry_run: DryRunOpt = False,
) -> None:
    """Divide un movimiento en 2-3 categorías; las partes deben sumar el total."""
    parsed = [parse_part(p) for p in parts]
    with _client() as client:
        known = client.categories()
        unknown = [cid for cid, _ in parsed if cid not in known]
        if unknown:
            raise typer.BadParameter(f"Categorías inexistentes: {', '.join(unknown)}")
        tx = client.transaction(tx_id)
        after = split_categories(tx, parsed)  # validates the sum even with --dry-run
        _change(
            tx, Field.category, after, partial(client.split, tx, parsed), command="split", dry_run=dry_run, cats=known
        )
    _finish(dry_run, f"Movimiento {escape(tx_id)} dividido en {len(parsed)} partes.")


@tx_app.command("describe")
def tx_describe(
    tx_id: Annotated[str, typer.Argument(help="id del movimiento.")],
    text: Annotated[str, typer.Argument(help="Concepto nuevo.")],
    dry_run: DryRunOpt = False,
) -> None:
    """Cambia el concepto que se muestra."""
    with _client() as client:
        tx = client.transaction(tx_id)
        write = partial(client.set_description, tx_id, text)
        _change(tx, Field.description, text, write, command="describe", dry_run=dry_run)
    _finish(dry_run)


@tx_app.command("note")
def tx_note(
    tx_id: Annotated[str, typer.Argument(help="id del movimiento.")],
    text: Annotated[str, typer.Argument(help="Nota nueva.")],
    dry_run: DryRunOpt = False,
) -> None:
    """Añade o cambia la nota del movimiento."""
    with _client() as client:
        tx = client.transaction(tx_id)
        _change(tx, Field.note, text, partial(client.set_note, tx_id, text), command="note", dry_run=dry_run)
    _finish(dry_run)


@tx_app.command("mark-read")
def tx_mark_read(
    tx_ids: Annotated[list[str] | None, typer.Argument(help="ids a marcar como leídos.")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Marcar todos los movimientos.")] = False,
    bank: Annotated[str | None, typer.Option("--bank", help="Marcar todos los de este banco.")] = None,
    product: Annotated[
        str | None, typer.Option("--product", help="Marcar todos los de esta cuenta o tarjeta (id de `products`).")
    ] = None,
    dry_run: DryRunOpt = False,
) -> None:
    """Marca como leídos unos movimientos, los de un banco (--bank), producto (--product) o todos (--all)."""
    targets = sum(bool(t) for t in (tx_ids, all_, bank, product))
    if targets > 1:
        raise typer.BadParameter("Usa solo una opción: ids, --bank, --product o --all.")
    if not targets:
        raise typer.BadParameter("Indica ids, --bank, --product o --all.")
    with _client() as client:
        if tx_ids:
            for tx_id in tx_ids:
                tx = client.transaction(tx_id)
                _change(tx, Field.read, True, partial(client.mark_read, tx_id), command="mark-read", dry_run=dry_run)
        else:
            _mark_many_read(client, bank=bank, product_id=product, dry_run=dry_run)
    _finish(dry_run)


def _mark_many_read(client: FintonicClient, *, bank: str | None, product_id: str | None, dry_run: bool) -> None:
    """Bulk endpoints don't say what they touched: list the unread ones first so each can be logged."""
    product = client.product(product_id) if product_id else None
    if product:
        product_read_path(product)  # fail on loans, funds... before listing, also with --dry-run
    unread = list(client.transactions(bank_id=bank, product=product, unread=True))
    for tx in unread:
        _print_change(tx.id, Field.read, False, True, None, dry_run=dry_run)
    err.print(f"{len(unread)} movimientos sin leer.")
    if dry_run:
        return
    if product:
        client.mark_product_read(product)
    else:
        client.mark_all_read(bank)
    scope = f"product:{product_id}" if product_id else f"bank:{bank}" if bank else "all"
    for tx in unread:
        _record(tx, Field.read, False, True, "mark-read", scope=scope)


@tx_app.command("mark-unread")
def tx_mark_unread(
    tx_ids: Annotated[list[str], typer.Argument(help="ids a marcar como no leídos.")],
    dry_run: DryRunOpt = False,
) -> None:
    """Marca movimientos como no leídos (la web no lo permite; endpoint de la app Android)."""
    with _client() as client:
        for tx_id in tx_ids:
            tx = client.transaction(tx_id)
            write = partial(client.mark_unread, tx_id)
            _change(tx, Field.read, False, write, command="mark-unread", dry_run=dry_run)
    _finish(dry_run)


@tx_app.command("set-date")
def tx_set_date(
    tx_id: Annotated[str, typer.Argument(help="id del movimiento.")],
    day: Annotated[str, typer.Argument(help="Fecha nueva YYYY-MM-DD.", metavar="YYYY-MM-DD")],
    dry_run: DryRunOpt = False,
) -> None:
    """Cambia la fecha del movimiento (la que se usa en listados y resúmenes)."""
    new_day = _parse_day(day)
    with _client() as client:
        tx = client.transaction(tx_id)
        write = partial(client.set_user_date, tx_id, new_day)
        _change(tx, Field.user_date, new_day.isoformat(), write, command="set-date", dry_run=dry_run)
    _finish(dry_run)


@tx_app.command("history")
def tx_history(
    tx_id: Annotated[str | None, typer.Argument(help="id del movimiento (vacío = todos).")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", min=0, help="Máximo de cambios (0 = todos).")] = 20,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Cambios hechos desde este equipo, del más reciente al más antiguo (no consulta la API)."""
    records = _changelog().history(tx_id, limit or None)
    if fmt is OutputFormat.json:
        print_json([asdict(r) for r in records])
        return
    rows = [
        {
            "fecha": r.moment.astimezone().strftime("%Y-%m-%d %H:%M"),
            "comando": f"{r.command} --similar" if r.rule else r.command,
            "id": r.tx_id,
            "campo": LABELS.get(r.field, r.field),
            "antes": describe_value(r.field, r.before),
            "después": describe_value(r.field, r.after),
            "ámbito": r.scope or "",
        }
        for r in records
    ]
    render(rows, fmt, title="Historial de cambios")


@tx_app.command("undo")
def tx_undo(
    tx_id: Annotated[str, typer.Argument(help="id del movimiento.")],
    dry_run: DryRunOpt = False,
) -> None:
    """Deshace el último cambio registrado de un movimiento (también se registra).

    No deshace `set-category --similar`: creó una regla en Fintonic que afecta a otros movimientos
    y este comando no la toca; se niega y no escribe nada. Revisa la regla en la app.
    """
    record = _changelog().last_for(tx_id)
    if record is None:
        raise no_history(tx_id)
    with _client() as client:
        tx = client.transaction(tx_id)
        cats = client.categories() if record.field == Field.category else None
        plan = plan_undo(record, tx, cats)
        if plan.noop:
            target = escape(describe_value(plan.field, plan.target, cats))
            err.print(f"Nada que deshacer: su {LABELS[plan.field]} ya es {target}.", soft_wrap=True)
            return
        write = partial(write_value, client, tx, plan.field, plan.target)
        _change(tx, plan.field, plan.target, write, command="undo", dry_run=dry_run, cats=cats, undoes=record.timestamp)
    _finish(dry_run, f"Deshecho el cambio {escape(record.command)} del {record.moment.astimezone():%Y-%m-%d %H:%M}.")


@app.command()
def refresh(
    bank: Annotated[str | None, typer.Option("--bank", help="Solo este banco (bank_id de `fintonic banks`).")] = None,
    wait: Annotated[bool, typer.Option(help="Esperar a que terminen de sincronizar.")] = True,
    timeout: Annotated[int, typer.Option(help="Segundos máximos de espera.")] = 300,
    ask_2fa: Annotated[
        bool,
        typer.Option("--2fa/--no-2fa", help="Si un banco pide 2FA, pedir aquí el código (solo en terminal)."),
    ] = True,
) -> None:
    """Pide a los bancos que sincronicen (Perfil > Entidades > Actualizar) y espera.

    Un banco cuenta como actualizado si se le ve sincronizar o si cambia su fecha de última
    sincronización. Sale con 2 si alguno sigue sincronizando al agotar --timeout, y con 3 si
    alguno acaba con error, con 2FA pendiente o rechazado, o sin constancia de haber sincronizado.
    """
    with _client() as client:
        since = client.last_sync_dates() if wait else None
        if bank:
            client.refresh_bank(bank)
        else:
            client.refresh_banks()
        if not wait:
            err.print("Actualización solicitada.")
            return
        wait_and_report(
            client,
            err,
            timeout=timeout,
            bank_ids=[bank] if bank else None,
            interactive=ask_2fa and _stdin_is_tty(),
            since=since,
        )


@app.command("bank-2fa")
def bank_2fa(
    bank_id: Annotated[str, typer.Argument(help="bank_id que espera 2FA (ver `fintonic refresh`).")],
    code: Annotated[
        str | None,
        typer.Argument(help="Código del banco. Sin código, confirma que ya lo aprobaste en la app del banco."),
    ] = None,
    wait: Annotated[bool, typer.Option(help="Esperar a que termine de sincronizar.")] = True,
    timeout: Annotated[int, typer.Option(help="Segundos máximos de espera.")] = 300,
) -> None:
    """Envía el código 2FA que pide un banco al sincronizar.

    Al esperar, sale con 2 si sigue sincronizando al agotar --timeout y con 3 si acaba con error o 2FA pendiente.
    """
    with _client() as client:
        aggregation = client.aggregation(bank_id)
        if not (needs_2fa(aggregation) or is_invalid_2fa(aggregation)):
            raise InputError(f"El banco {bank_id} no está esperando 2FA.")
        request = SecondFactorRequest.from_aggregation(aggregation)
        if code is None and not request.push:
            if not _stdin_is_tty():
                raise InputError(f"El banco {bank_id} pide un código: `fintonic bank-2fa {bank_id} CODIGO`.")
            code = ask_second_factor(err, request)
            if code is None:
                err.print("Sin código: no se envía nada.")
                return
        client.submit_second_factor(request.aggregation_id, code)
        err.print("Respuesta enviada al banco.")
        if wait:
            answered = {bank_id: aggregation.get("status")}
            wait_and_report(client, err, timeout=timeout, bank_ids=[bank_id], answered=answered)


def main() -> None:
    try:
        app()
    except FintonicError as exc:
        err.print(f"[red]Error:[/red] {escape(str(exc))}")
        raise SystemExit(1) from exc
    except httpx.HTTPError as exc:
        err.print(f"[red]Error de red:[/red] {type(exc).__name__}. Fintonic no responde; reintenta en un rato.")
        raise SystemExit(1) from exc
