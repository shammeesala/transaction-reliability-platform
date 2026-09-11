"""Request, response, and RFC 7807 problem detail schemas for the emulator."""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator

CURRENCY_FORMAT_REGEX = re.compile(r"^[A-Z]{3}$")


class InvalidParameter(BaseModel):
    """RFC 7807 field-level invalid parameter description."""

    field: str
    reason: str


class ProblemDetail(BaseModel):
    """RFC 7807 problem details object."""

    type: str
    title: str
    status: int
    detail: str
    invalid_parameters: list[InvalidParameter] | None = None


class AuthorizationRequest(BaseModel):
    """Inbound authorization request to the payment network emulator.

    Validates format for ISO 4217 three-letter codes without asserting membership.
    Extra fields (such as raw PAN or CVV) are strictly forbidden.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str
    merchant_id: str
    payment_token: str
    amount: int
    currency: str

    @field_validator("request_id", "merchant_id", "payment_token", mode="before")
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


class AuthorizedResponse(BaseModel):
    """Successful payment network authorization response."""

    outcome: Literal["AUTHORIZED"] = "AUTHORIZED"
    network_reference: str
    authorization_code: str = "AUTH123"


class DeclinedResponse(BaseModel):
    """Handled business outcome decline response."""

    outcome: Literal["DECLINED"] = "DECLINED"
    decline_code: str = "INSUFFICIENT_FUNDS"
