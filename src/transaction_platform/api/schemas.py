"""Request and response schemas for the transaction API."""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

CURRENCY_FORMAT_REGEX = re.compile(r"^[A-Z]{3}$")


class CreateTransactionRequest(BaseModel):
    """Inbound authorization request for a transaction.

    Accepts only synthetic tok_test_* payment tokens.
    Strictly forbids raw card details (PAN, CVV, expiration date, cardholder name)
    and unknown parameters.
    """

    model_config = ConfigDict(extra="forbid")

    transaction_id: str
    merchant_id: str
    payment_token: str
    amount: int
    currency: str

    @field_validator("transaction_id", "merchant_id", mode="before")
    @classmethod
    def validate_stripped_nonblank_string(cls, v: Any, info: ValidationInfo) -> str:
        field_name = info.field_name or "field"
        if not isinstance(v, str):
            raise ValueError(f"{field_name} must be a string")
        stripped = v.strip()
        if not stripped:
            raise ValueError(f"{field_name} must not be blank")
        if len(stripped) > 128:
            raise ValueError(f"{field_name} must not exceed 128 characters")
        return stripped

    @field_validator("payment_token", mode="before")
    @classmethod
    def validate_payment_token(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError("payment_token must be a string")
        stripped = v.strip()
        if not stripped:
            raise ValueError("payment_token must not be blank")
        if len(stripped) > 128:
            raise ValueError("payment_token must not exceed 128 characters")
        if not stripped.startswith("tok_test_"):
            raise ValueError("payment_token must be a synthetic test token starting with tok_test_")
        return stripped

    @field_validator("currency")
    @classmethod
    def validate_currency_format(cls, v: str) -> str:
        if not CURRENCY_FORMAT_REGEX.match(v):
            raise ValueError(
                "currency must be exactly three uppercase ASCII letters (ISO 4217 format)"
            )
        return v

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount(cls, v: Any) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("amount must be an integer")
        if v <= 0:
            raise ValueError("amount must be greater than zero")
        return v


class AuthorizedTransactionResponse(BaseModel):
    """Returned when the payment network confirmed authorization succeeded."""

    transaction_id: str
    status: Literal["AUTHORIZED"] = "AUTHORIZED"
    version: int = Field(ge=0)
    network_reference: str
    authorization_code: str


class DeclinedTransactionResponse(BaseModel):
    """Returned when the payment network confirmed a business decline."""

    transaction_id: str
    status: Literal["DECLINED"] = "DECLINED"
    version: int = Field(ge=0)
    decline_code: str


class PendingReconciliationTransactionResponse(BaseModel):
    """Returned when the downstream outcome is ambiguous and requires reconciliation."""

    transaction_id: str
    status: Literal["PENDING_RECONCILIATION"] = "PENDING_RECONCILIATION"
    version: int = Field(ge=0)
    pending_operation: Literal["AUTHORIZATION"] = "AUTHORIZATION"
