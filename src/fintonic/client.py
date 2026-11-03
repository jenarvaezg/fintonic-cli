"""High-level Fintonic API client."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import date
from itertools import islice
from typing import Any, Self

import httpx

from fintonic.aggregation import AggregationStatus, evaluate
from fintonic.config import API_BASE, BFF_BASE
from fintonic.errors import InputError, NotLoggedInError, SessionExpiredError, raise_for_response
from fintonic.headers import auth_headers
from fintonic.http import build_http_client
from fintonic.models import BankRegistry, Category, Position, Product, Transaction, TransactionDateRange
from fintonic.session import Session
from fintonic.split import build_split_body
from fintonic.stores import SessionStore

PRODUCT_ENDPOINTS: Mapping[str, str] = {
    "account": "rest/account/listAccounts",
    "credit_card": "rest/creditcard/listCreditCards",
    "deposit": "rest/deposit/listDeposits",
    "fund": "rest/fund/listFunds",
    "loan": "rest/loan/listLoans",
    "share": "rest/share/listShares",
    "pension_plan": "rest/pensionplan/listPensionPlans",
}

TRANSACTIONS_PATH = "rest/transaction/list/pageable"
TRANSACTION_PRODUCT_KINDS = frozenset({"account", "credit_card"})  # the only kinds with movements
DEFAULT_PAGE_SIZE = 100  # the server caps `size` at 100

Relogin = Callable[[Session], Session]


class FintonicClient:
    def __init__(
        self,
        session: Session,
        *,
        http: httpx.Client | None = None,
        store: SessionStore | None = None,
        relogin: Relogin | None = None,
    ) -> None:
        if not session.access_token:
            raise NotLoggedInError()
        self._session = session
        self._http = http or build_http_client()
        self._owns_http = http is None
        self._store = store
        self._relogin = relogin

    @property
    def session(self) -> Session:
        return self._session

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---- transport ---------------------------------------------------------

    def get(self, path: str, params: Any = None, *, bff: bool = False) -> Any:
        base = BFF_BASE if bff else API_BASE
        return self._request("GET", f"{base}/{path.lstrip('/')}", params=params)

    def post(self, path: str, body: Any = None, *, bff: bool = False) -> Any:
        """Not retried by the transport: writes must not be replayed blindly."""
        base = BFF_BASE if bff else API_BASE
        return self._request("POST", f"{base}/{path.lstrip('/')}", json=body)

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        refreshed = self._session.is_expired()
        if refreshed:
            self._refresh_session()
        response = self._send(method, url, **kwargs)
        if response.status_code == 401 and self._relogin and not refreshed:
            self._refresh_session()
            response = self._send(method, url, **kwargs)
        raise_for_response(response)
        return response.json() if response.content else None

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return self._http.request(method, url, headers=auth_headers(self._session), **kwargs)

    def _refresh_session(self) -> None:
        if not self._relogin:
            raise SessionExpiredError()
        self._session = self._relogin(self._session)
        if self._store:
            self._store.save(self._session)

    # ---- user --------------------------------------------------------------

    def profile(self) -> dict[str, Any]:
        return self.get("rest/user/profile")

    # ---- banks & products --------------------------------------------------

    def banks(self) -> list[BankRegistry]:
        body = self.get("rest/bankRegistries")
        return [BankRegistry.model_validate(b) for b in body["data"]["bankRegistries"]]

    def aggregations(self) -> list[dict[str, Any]]:
        """Open-banking sync status per bank (BFF)."""
        return self.get("openbanking/aggregations/", bff=True)

    def refresh_banks(self) -> None:
        """Ask every connected bank to sync, like Perfil > Entidades > Actualizar."""
        self.post("openbanking/aggregations/data-actualizations", bff=True)

    def wait_for_banks(
        self, *, timeout: float = 300, interval: float = 2, sleep: Callable[[float], None] = time.sleep
    ) -> AggregationStatus:
        """Poll until no bank is syncing (banks waiting for 2FA are reported, not awaited)."""
        deadline = time.monotonic() + timeout
        while True:
            status = evaluate(self.aggregations())
            if status.done or time.monotonic() >= deadline:
                return status
            sleep(interval)

    def products(self, kind: str) -> list[Product]:
        try:
            endpoint = PRODUCT_ENDPOINTS[kind]
        except KeyError as exc:
            raise InputError(f"Tipo de producto desconocido: {kind}. Opciones: {', '.join(PRODUCT_ENDPOINTS)}") from exc
        return [Product.model_validate({**raw, "kind": kind}) for raw in _flatten_by_bank(self.get(endpoint))]

    def all_products(self, kinds: Iterable[str] = PRODUCT_ENDPOINTS) -> list[Product]:
        return [p for kind in kinds for p in self.products(kind)]

    def product(self, product_id: str) -> Product:
        """Look up any product (live or canceled) by the `id` shown in `fintonic products`."""
        for p in self.all_products():
            if p.id == product_id:
                return p
        raise InputError(f"No existe el producto {product_id}. Mira los ids con `fintonic products --all`.")

    def position(self) -> Position:
        return Position.model_validate(self.get("rest/overview/position")["data"])

    # ---- categories --------------------------------------------------------

    def categories(self) -> dict[str, Category]:
        tree = self.get("rest/category/tree")["categoryTree"]
        return {cid: Category.model_validate(raw) for cid, raw in tree.items()}

    # ---- transactions ------------------------------------------------------

    def transaction(self, tx_id: str) -> Transaction:
        body = self.get("rest/transaction/listByIds", [("transactionIds", tx_id)])
        for raw in body.get("resultList") or []:
            if raw.get("id") == tx_id:
                return Transaction.model_validate(raw)
        raise InputError(f"No existe el movimiento {tx_id}.")

    def set_category(self, tx_id: str, category_id: str, *, apply_to_similar: bool = False) -> None:
        """Recategorize one movement; `apply_to_similar` creates a rule (web: "aplicar a todos")."""
        action = "applyUserRule" if apply_to_similar else "category"
        self.post(f"rest/transaction/{tx_id}/{action}", {"categoryId": category_id})

    def set_description(self, tx_id: str, text: str) -> None:
        self.post(f"rest/transaction/{tx_id}/primaryDisplay", {"textFieldValue": text})

    def set_note(self, tx_id: str, text: str) -> None:
        self.post(f"rest/transaction/{tx_id}/secondaryDisplay", {"textFieldValue": text})

    def split(self, tx: Transaction, parts: list[tuple[str, int]]) -> None:
        """Split `tx` into (category_id, positive cents) parts that must add up to its amount."""
        self.post(f"rest/transaction/{tx.id}/split", build_split_body(tx.quantity, parts))

    def mark_read(self, tx_id: str) -> None:
        self.post(f"rest/transaction/{tx_id}/markAsRead")

    def mark_all_read(self, bank_id: str | None = None) -> None:
        self.post(f"rest/transaction/markAsReadByBank/{bank_id}" if bank_id else "rest/transaction/markAsReadByUser")

    def transaction_date_range(self) -> TransactionDateRange:
        return TransactionDateRange.model_validate(self.get("rest/transaction/olderNewerTransactionUserDate"))

    def transactions(
        self,
        *,
        begin: date | None = None,
        end: date | None = None,
        category_ids: Sequence[str] = (),
        query: str | None = None,
        bank_id: str | None = None,
        product: Product | None = None,
        unread: bool = False,
        min_amount: int | None = None,
        max_amount: int | None = None,
        unclassified: bool = False,
        direct_debit: bool = False,
        oldest_first: bool = False,
        limit: int | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Iterator[Transaction]:
        """Iterate transactions newest-first (or oldest-first), paginating transparently.

        Every filter runs server-side. `category_ids` match any of them; `min_amount` and
        `max_amount` are signed cents (expenses are negative). Only accounts and credit cards
        have movements: any other `product` yields nothing.
        """
        product_filter = _product_filter(product) if product else {}
        if product_filter is None:
            return
        filters: list[tuple[str, Any]] = [
            *_compact(
                {
                    "beginDate": begin.isoformat() if begin else None,
                    "endDate": end.isoformat() if end else None,
                    "queryText": query,
                    "bankId": bank_id,
                    "read": "false" if unread else None,
                    "minAmount": min_amount,
                    "maxAmount": max_amount,
                    "unclassified": "true" if unclassified else None,
                    "onlyDirectDebit": "true" if direct_debit else None,
                    **product_filter,
                }
            ).items(),
            *(("categoryId", cid) for cid in category_ids),
            ("sort", f"userDate,{'asc' if oldest_first else 'desc'}"),
        ]
        size = min(page_size, limit) if limit else page_size
        yield from islice(self._paginate(TRANSACTIONS_PATH, filters, size), limit)

    def _paginate(self, path: str, filters: list[tuple[str, Any]], size: int) -> Iterator[Transaction]:
        """Walk a Spring page response (`content`, `last`) until the last or an empty page."""
        page = 0
        while True:
            body = self.get(path, [*filters, ("size", size), ("page", page)])
            items = body.get("content") or []
            yield from (Transaction.model_validate(raw) for raw in items)
            if not items or body.get("last", True):
                return
            page += 1


def _product_filter(product: Product) -> dict[str, str] | None:
    """Server-side filter for a product's movements, or None if it has none (loans, funds...)."""
    if product.kind == "account" and product.ccc:
        return {"ccc": product.ccc}
    card = product.card_number or product.display_card_number
    if product.kind == "credit_card" and card:
        return {"cardNumber": card}
    return None


def _compact(params: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


def _flatten_by_bank(body: Any) -> list[dict[str, Any]]:
    """Product endpoints return `{"<kind>s": {"<bankId>": [product, ...]}}`."""
    if not isinstance(body, dict):
        return []
    return [
        product
        for by_bank in body.values()
        if isinstance(by_bank, dict)
        for products in by_bank.values()
        if isinstance(products, list)
        for product in products
    ]
