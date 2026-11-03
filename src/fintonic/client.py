"""High-level Fintonic API client."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from datetime import date
from typing import Any, Self

import httpx

from fintonic.config import API_BASE, BFF_BASE
from fintonic.errors import NotLoggedInError, SessionExpiredError, raise_for_response
from fintonic.http import build_http_client
from fintonic.models import BankRegistry, Category, Position, Product, Transaction, TransactionDateRange
from fintonic.session import Session, SessionStore

PRODUCT_ENDPOINTS: Mapping[str, str] = {
    "account": "rest/account/listAccounts",
    "credit_card": "rest/creditcard/listCreditCards",
    "deposit": "rest/deposit/listDeposits",
    "fund": "rest/fund/listFunds",
    "loan": "rest/loan/listLoans",
    "share": "rest/share/listShares",
    "pension_plan": "rest/pensionplan/listPensionPlans",
}

DEFAULT_PAGE_SIZE = 100

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

    def get(self, path: str, params: Mapping[str, Any] | None = None, *, bff: bool = False) -> Any:
        base = BFF_BASE if bff else API_BASE
        return self._request("GET", f"{base}/{path.lstrip('/')}", params=params)

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        if self._session.is_expired():
            self._refresh_session()
        response = self._send(method, url, **kwargs)
        if response.status_code == 401 and self._relogin:
            self._refresh_session()
            response = self._send(method, url, **kwargs)
        raise_for_response(response)
        return response.json() if response.content else None

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {
            "authorization": f"Bearer {self._session.access_token}",
            "device-uuid": self._session.device_uuid or "",
        }
        return self._http.request(method, url, headers=headers, **kwargs)

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

    def products(self, kind: str) -> list[Product]:
        try:
            endpoint = PRODUCT_ENDPOINTS[kind]
        except KeyError as exc:
            raise ValueError(f"Tipo de producto desconocido: {kind}. Opciones: {', '.join(PRODUCT_ENDPOINTS)}") from exc
        return [Product.model_validate({**raw, "kind": kind}) for raw in _flatten_by_bank(self.get(endpoint))]

    def all_products(self) -> list[Product]:
        return [p for kind in PRODUCT_ENDPOINTS for p in self.products(kind)]

    def position(self) -> Position:
        return Position.model_validate(self.get("rest/overview/position")["data"])

    # ---- categories --------------------------------------------------------

    def categories(self) -> dict[str, Category]:
        tree = self.get("rest/category/tree")["categoryTree"]
        return {cid: Category.model_validate(raw) for cid, raw in tree.items()}

    # ---- transactions ------------------------------------------------------

    def transaction_date_range(self) -> TransactionDateRange:
        return TransactionDateRange.model_validate(self.get("rest/transaction/olderNewerTransactionUserDate"))

    def transactions(
        self,
        *,
        begin: date | None = None,
        end: date | None = None,
        category_id: str | None = None,
        query: str | None = None,
        bank_id: str | None = None,
        limit: int | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Iterator[Transaction]:
        """Iterate transactions newest-first, paginating transparently."""
        path = f"rest/transaction/listByBank/{bank_id}" if bank_id else "rest/transaction/list"
        filters = _compact(
            {
                "beginDate": begin.isoformat() if begin else None,
                "endDate": end.isoformat() if end else None,
                "categoryId": category_id,
                "queryText": query,
            }
        )
        offset = 0
        yielded = 0
        while True:
            body = self.get(path, {**filters, "pageLimit": page_size, "pageOffset": offset})
            items = body.get("resultList") or []
            for raw in items:
                yield Transaction.model_validate(raw)
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            offset += len(items)
            if not items or offset >= int(body.get("count") or 0):
                return


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
