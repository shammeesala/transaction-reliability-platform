"""RFC 7807 problem details exception handlers for the transaction API."""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import Response

from transaction_platform.application.authorize_transaction import (
    DownstreamRateLimitExceededError,
)
from transaction_platform.common.problem_details import (
    InvalidParameter,
    build_problem_response,
    map_safe_field_name,
)

__all__ = [
    "downstream_rejected_handler",
    "missing_correlation_id_handler",
    "missing_idempotency_key_handler",
    "persistence_error_handler",
    "rate_limit_exceeded_handler",
    "validation_exception_handler",
]


async def rate_limit_exceeded_handler(
    request: Request, exc: Exception
) -> Response:
    """Handle explicit downstream rate limiting with HTTP 503 and safe Retry-After."""
    retry_after = 1
    if isinstance(exc, DownstreamRateLimitExceededError):
        retry_after = exc.retry_after

    return build_problem_response(
        status_code=503,
        problem_type="urn:problem-type:provider-rate-limited",
        title="Downstream rate limit exceeded",
        detail="Downstream payment network rate limit exceeded.",
        headers={"Retry-After": str(retry_after)},
    )


async def downstream_rejected_handler(
    request: Request, exc: Exception
) -> Response:
    """Handle explicit downstream request rejection without echoing upstream details."""
    return build_problem_response(
        status_code=502,
        problem_type="urn:problem-type:downstream-rejected",
        title="Payment network request rejected",
        detail="The payment network rejected the authorization request.",
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


async def missing_idempotency_key_handler(
    request: Request, exc: Exception
) -> Response:
    """Handle missing or malformed Idempotency-Key headers."""
    return build_problem_response(
        status_code=400,
        problem_type="urn:problem-type:missing-idempotency-key",
        title="Missing or invalid idempotency key",
        detail=(
            "The Idempotency-Key header is required and must match "
            "pattern [A-Za-z0-9._:-]+ up to 128 characters without whitespace."
        ),
    )


async def persistence_error_handler(
    request: Request, exc: Exception
) -> Response:
    """Handle unexpected persistence or concurrency failures with a sanitized RFC 7807 500 response.

    Strictly forbids exposing SQL queries, table/constraint names, hashes, or driver internals.
    """
    return build_problem_response(
        status_code=500,
        problem_type="urn:problem-type:internal-persistence-error",
        title="Internal Server Error",
        detail="A persistence error occurred while processing the transaction.",
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
