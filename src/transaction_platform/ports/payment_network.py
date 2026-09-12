"""Abstract port and domain failure types for downstream payment network integrations."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PaymentNetworkAuthorizationRequest:
    """Request command submitted to the payment network port."""

    transaction_id: str
    merchant_id: str
    payment_token: str
    amount: int
    currency: str
    correlation_id: str


@dataclass(frozen=True)
class PaymentNetworkAuthorizedResult:
    """The payment network confirmed the authorization succeeded."""

    network_reference: str
    authorization_code: str


@dataclass(frozen=True)
class PaymentNetworkDeclinedResult:
    """The payment network returned a handled business decline outcome."""

    decline_code: str


class PaymentNetworkError(Exception):
    """Base exception for payment network integration failures."""


class PaymentNetworkTimeoutError(PaymentNetworkError):
    """Downstream network or read timeout occurred after request dispatch."""


class PaymentNetworkRateLimitError(PaymentNetworkError):
    """Downstream explicitly returned rate limiting (HTTP 429)."""

    def __init__(self, retry_after: int = 1) -> None:
        super().__init__("Downstream payment network rate limit exceeded.")
        self.retry_after = retry_after


class PaymentNetworkInvalidResponseError(PaymentNetworkError):
    """Downstream returned malformed JSON or contract-invalid response fields."""


class PaymentNetworkUnavailableError(PaymentNetworkError):
    """Downstream connection dropped, unreachable, or returned a 5xx server error."""


class PaymentNetworkRejectedError(PaymentNetworkError):
    """Downstream explicitly rejected the request prior to processing (HTTP 4xx non-429)."""


class PaymentNetworkPort(Protocol):
    """Abstract port representing payment network authorization operations."""

    async def authorize(
        self, request: PaymentNetworkAuthorizationRequest
    ) -> PaymentNetworkAuthorizedResult | PaymentNetworkDeclinedResult:
        """Process authorization with the downstream network.

        Raises:
            PaymentNetworkTimeoutError: On read/connect timeout.
            PaymentNetworkRateLimitError: On downstream HTTP 429.
            PaymentNetworkInvalidResponseError: On malformed/contract-invalid 200.
            PaymentNetworkUnavailableError: On transport drops/disconnects or 5xx.
            PaymentNetworkRejectedError: On explicit downstream 400/422 rejection.
        """
        ...
