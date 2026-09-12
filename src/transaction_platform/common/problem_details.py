"""Shared RFC 7807 problem details models and response builders."""

from fastapi.responses import JSONResponse
from pydantic import BaseModel

PROBLEM_MEDIA_TYPE = "application/problem+json"

SAFE_SCHEMA_FIELDS = frozenset({
    "transaction_id",
    "request_id",
    "merchant_id",
    "payment_token",
    "amount",
    "currency",
    "body",
})

FORBIDDEN_SENSITIVE_FIELDS = frozenset({
    "pan",
    "cvv",
    "card_number",
    "security_code",
    "expiration_date",
    "cardholder_name",
})


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


def map_safe_field_name(raw_field: str) -> str:
    """Map a field name from validation errors to a safe non-reflecting label.

    Prevents arbitrary field names or sensitive data embedded in JSON keys from being echoed.
    """
    normalized = raw_field.strip().lower()
    if normalized in SAFE_SCHEMA_FIELDS:
        return normalized
    if normalized in FORBIDDEN_SENSITIVE_FIELDS:
        return "sensitive_parameter"
    return "unknown_parameter"


def build_problem_response(
    status_code: int,
    problem_type: str,
    title: str,
    detail: str,
    invalid_parameters: list[InvalidParameter] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Construct an RFC 7807 compliant JSONResponse."""
    problem = ProblemDetail(
        type=problem_type,
        title=title,
        status=status_code,
        detail=detail,
        invalid_parameters=invalid_parameters,
    )
    return JSONResponse(
        status_code=status_code,
        content=problem.model_dump(exclude_none=True),
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )
