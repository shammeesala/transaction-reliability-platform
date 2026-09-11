"""Payment network emulator scenario selection and deterministic references."""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable

from starlette.responses import Response

from transaction_platform.emulator.errors import (
    RateLimitedError,
    UnsupportedTokenError,
)
from transaction_platform.emulator.schemas import (
    AuthorizationRequest,
    AuthorizedResponse,
    DeclinedResponse,
)

TOKEN_APPROVED = "tok_test_approved"
TOKEN_DECLINED = "tok_test_declined"
TOKEN_RATE_LIMITED = "tok_test_rate_limited"
TOKEN_MALFORMED = "tok_test_malformed"
TOKEN_TIMEOUT = "tok_test_timeout"

SUPPORTED_TOKENS = frozenset({
    TOKEN_APPROVED,
    TOKEN_DECLINED,
    TOKEN_RATE_LIMITED,
    TOKEN_MALFORMED,
    TOKEN_TIMEOUT,
})

AsyncSleeper = Callable[[float], Awaitable[None]]


async def default_async_sleeper(delay: float) -> None:
    """Default asynchronous sleeper using asyncio.sleep."""
    await asyncio.sleep(delay)


def get_async_sleeper() -> AsyncSleeper:
    """Dependency provider returning the default asynchronous sleeper."""
    return default_async_sleeper


def derive_network_reference(request_id: str) -> str:
    """Derive a deterministic network reference from a request ID.

    Characteristics:
    - The same request ID deterministically produces the same reference.
    - Different representative request IDs produce different references in tests.
    - SHA-256 provides stable, collision-resistant simulation behavior.
    - This is an emulator simulation, not a production network-identifier allocation system.
    - The emulator validates request_id format and presence; the caller owns global uniqueness.
    """
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:16]
    return f"net_{digest}"


async def execute_authorization_scenario(
    request: AuthorizationRequest,
    timeout_delay: float,
    sleeper: AsyncSleeper,
) -> Response | AuthorizedResponse | DeclinedResponse:
    """Select and execute the scenario corresponding to the payment token.

    Raises:
        UnsupportedTokenError: If payment_token is not recognized.
        RateLimitedError: If payment_token is tok_test_rate_limited.
    """
    token = request.payment_token

    if token not in SUPPORTED_TOKENS:
        raise UnsupportedTokenError()

    if token == TOKEN_APPROVED:
        return AuthorizedResponse(
            outcome="AUTHORIZED",
            network_reference=derive_network_reference(request.request_id),
            authorization_code="AUTH123",
        )

    if token == TOKEN_DECLINED:
        return DeclinedResponse(
            outcome="DECLINED",
            decline_code="INSUFFICIENT_FUNDS",
        )

    if token == TOKEN_RATE_LIMITED:
        raise RateLimitedError()

    if token == TOKEN_MALFORMED:
        # Emulator contract-violation scenario: return deliberately unparseable JSON
        return Response(
            content='{"outcome": "AUTHORIZED", network_reference:',
            media_type="application/json",
            status_code=200,
        )

    if token == TOKEN_TIMEOUT:
        await sleeper(timeout_delay)
        return AuthorizedResponse(
            outcome="AUTHORIZED",
            network_reference=derive_network_reference(request.request_id),
            authorization_code="AUTH123",
        )

    raise UnsupportedTokenError()
