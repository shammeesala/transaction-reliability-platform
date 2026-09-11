"""Domain errors and RFC 7807 exception handlers for the emulator."""

import re

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.responses import Response

from transaction_platform.emulator.schemas import InvalidParameter, ProblemDetail

CORRELATION_ID_REGEX = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

PROBLEM_MEDIA_TYPE = "application/problem+json"

SAFE_SCHEMA_FIELDS = frozenset({
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


class EmulatorError(Exception):
    """Base exception for payment network emulator errors."""


class MissingCorrelationIdError(EmulatorError):
    """Raised when X-Correlation-ID header is missing, blank, or invalid."""


class UnsupportedTokenError(EmulatorError):
    """Raised when a payment token is not supported by the emulator scenarios."""


class RateLimitedError(EmulatorError):
    """Raised when simulating downstream payment network rate limiting."""


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


async def missing_correlation_id_handler(
    request: Request, exc: Exception
) -> Response:
    """Handle missing or malformed correlation ID headers."""
    return build_problem_response(
        status_code=400,
        problem_type="urn:problem-type:missing-correlation-id",
        title="Missing or invalid correlation ID",
        detail=(
            "The X-Correlation-ID header is required and must match "
            "pattern [A-Za-z0-9._:-]+ up to 128 characters without whitespace."
        ),
    )


async def unsupported_token_handler(
    request: Request, exc: Exception
) -> Response:
    """Handle unsupported payment tokens without echoing the rejected token."""
    return build_problem_response(
        status_code=422,
        problem_type="urn:problem-type:unsupported-token",
        title="Unsupported payment token",
        detail="The provided payment token is not supported by the payment network emulator.",
    )


async def rate_limited_handler(
    request: Request, exc: Exception
) -> Response:
    """Handle simulated rate limiting."""
    return build_problem_response(
        status_code=429,
        problem_type="urn:problem-type:rate-limited",
        title="Rate limit exceeded",
        detail="Payment network emulator simulated rate limit.",
        headers={"Retry-After": "1"},
    )


async def validation_exception_handler(
    request: Request, exc: Exception
) -> Response:
    """Handle request validation errors, distinguishing JSON decoding from schema issues."""
    if not isinstance(exc, RequestValidationError):
        return build_problem_response(
            status_code=400,
            problem_type="urn:problem-type:malformed-request",
            title="Malformed request",
            detail="The request could not be processed due to a formatting error.",
        )

    errors = exc.errors()

    is_json_decode_error = any(
        err.get("type") == "json_invalid" for err in errors
    )

    if is_json_decode_error:
        return build_problem_response(
            status_code=400,
            problem_type="urn:problem-type:malformed-request",
            title="Malformed JSON request",
            detail="The request body contains invalid JSON and could not be parsed.",
        )

    invalid_parameters: list[InvalidParameter] = []
    for err in errors:
        loc = err.get("loc", ())
        field_parts = [str(part) for part in loc if part != "body"]
        raw_field = field_parts[0] if field_parts else "body"
        safe_field = map_safe_field_name(raw_field)

        raw_msg = str(err.get("msg", "Invalid value"))
        prefix = "Value error, "
        clean_msg = raw_msg[len(prefix):] if raw_msg.startswith(prefix) else raw_msg

        invalid_parameters.append(
            InvalidParameter(field=safe_field, reason=clean_msg)
        )

    return build_problem_response(
        status_code=422,
        problem_type="urn:problem-type:validation-error",
        title="Validation error",
        detail="The request body failed schema validation.",
        invalid_parameters=invalid_parameters,
    )
