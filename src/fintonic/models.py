"""Typed views over Fintonic payloads. Amounts are integer cents in the API.

Models allow extra fields so new API attributes don't break parsing; the raw
payload stays reachable through `model_extra`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow", frozen=True)


def cents_to_decimal(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


class Categorization(_ApiModel):
    weighted_categories: dict[str, int] = Field(default_factory=dict)
    classified: bool = False
    categorized_by_user_rule: bool = False
    categorized_by_client: bool = False
    categorized_by_system: bool = False
    splited: bool = False


class Transaction(_ApiModel):
    id: str
    bank_id: str
    type: str
    product_id: str
    description: str = ""
    quantity: int
    currency: str = "EURO"
    value_date: date | None = None
    user_date: date | None = None
    operation_date: date | None = None
    note: str | None = None
    user_description: str | None = None
    primary_display: str = ""
    secondary_display: str = ""
    read: bool = False
    active: bool = True
    categorization: Categorization | None = None

    @property
    def amount(self) -> Decimal:
        return cents_to_decimal(self.quantity)

    @property
    def category_id(self) -> str | None:
        weights = self.categorization.weighted_categories if self.categorization else {}
        return max(weights, key=weights.__getitem__) if weights else None

    @property
    def label(self) -> str:
        return self.user_description or self.primary_display or self.description


class Category(_ApiModel):
    id: str
    key: str = ""
    name: str = ""
    shortname: str = ""
    emoji: str | None = None
    color: str | None = None
    children: list[str] = Field(default_factory=list)
    ancestors: list[str] = Field(default_factory=list)
    classified: bool = False
    computable: bool = True


class Balances(_ApiModel):
    """Balances by product family, in cents."""

    account_balance: int = 0
    credit_card_balance: int = 0
    deposit_balance: int = 0
    share_balance: int = 0
    fund_balance: int = 0
    loan_balance: int = 0
    pension_plan_balance: int = 0

    @property
    def net_worth(self) -> int:
        return sum(
            (
                self.account_balance,
                self.credit_card_balance,
                self.deposit_balance,
                self.share_balance,
                self.fund_balance,
                self.loan_balance,
                self.pension_plan_balance,
            )
        )


class Position(_ApiModel):
    position: Balances
    historic_position: dict[str, Balances] = Field(default_factory=dict)

    def history(self) -> list[tuple[str, Balances]]:
        """Monthly history sorted chronologically (the API returns it unordered)."""
        return sorted(self.historic_position.items())


class ProductStatus(_ApiModel):
    type: str
    ok: bool = True
    last_update: int | None = None


class BankRegistry(_ApiModel):
    bank_id: str
    system_bank_id: str | None = None
    name: str = ""
    active: bool = True
    aggregating: bool = False
    last_update_date: int | None = None
    products: list[ProductStatus] = Field(default_factory=list)


class Product(_ApiModel):
    """Account, card, loan, fund... All share this core; type-specific fields stay in `model_extra`."""

    kind: str
    id: str
    bank_id: str
    balance: int = 0
    currency: str = "EURO"
    active: bool = True
    canceled: bool = False
    alias: str | None = None
    user_alias: str | None = None
    web_alias: str | None = None
    display_ccc: str | None = None
    display_card_number: str | None = None
    last_update: int | None = None

    @property
    def name(self) -> str:
        return self.user_alias or self.alias or self.web_alias or ""

    @property
    def masked_number(self) -> str:
        """Only the already-masked numbers the API provides for display."""
        return self.display_ccc or self.display_card_number or ""

    @property
    def is_live(self) -> bool:
        return self.active and not self.canceled


class TransactionDateRange(_ApiModel):
    older_transaction_user_date: date | None = None
    newer_transaction_user_date: date | None = None
