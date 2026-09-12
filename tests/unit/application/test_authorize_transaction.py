"""Unit tests for AuthorizeTransactionService and orchestration domain transitions."""

import asyncio

import pytest

from transaction_platform.application.authorize_transaction import (
    AuthorizeTransactionCommand,
    AuthorizeTransactionService,
    DownstreamRateLimitExceededError,
    DownstreamRejectedError,
)
from transaction_platform.domain.transaction import (
    PendingOperation,
    TransactionStatus,
)
from transaction_platform.ports.payment_network import (
    PaymentNetworkAuthorizationRequest,
    PaymentNetworkAuthorizedResult,
    PaymentNetworkDeclinedResult,
    PaymentNetworkInvalidResponseError,
    PaymentNetworkPort,
    PaymentNetworkRateLimitError,
    PaymentNetworkRejectedError,
    PaymentNetworkTimeoutError,
    PaymentNetworkUnavailableError,
)


class MockPaymentNetworkPort(PaymentNetworkPort):
    """Test fake for PaymentNetworkPort capturing requests and returning canned outcomes."""

    def __init__(
        self,
        outcome: (
            PaymentNetworkAuthorizedResult
            | PaymentNetworkDeclinedResult
            | Exception
        ),
    ) -> None:
        self.outcome = outcome
        self.call_count = 0
        self.last_request: PaymentNetworkAuthorizationRequest | None = None

    async def authorize(
        self, request: PaymentNetworkAuthorizationRequest
    ) -> PaymentNetworkAuthorizedResult | PaymentNetworkDeclinedResult:
        self.call_count += 1
        self.last_request = request
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def make_command(
    transaction_id: str = "txn_test_100",
    merchant_id: str = "merchant_101",
    payment_token: str = "tok_test_approved",
    amount: int = 2500,
    currency: str = "USD",
    correlation_id: str = "corr_test_100",
) -> AuthorizeTransactionCommand:
    return AuthorizeTransactionCommand(
        transaction_id=transaction_id,
        merchant_id=merchant_id,
        payment_token=payment_token,
        amount=amount,
        currency=currency,
        correlation_id=correlation_id,
    )


class TestAuthorizeTransactionService:
    """Verify application orchestration and failure-to-state mappings."""

    def test_authorized_outcome_transitions_to_authorized(self) -> None:
        canned = PaymentNetworkAuthorizedResult(
            network_reference="net_abc_123",
            authorization_code="AUTH999",
        )
        port = MockPaymentNetworkPort(outcome=canned)
        service = AuthorizeTransactionService(payment_network=port)
        cmd = make_command()

        result = asyncio.run(service.execute(cmd))

        assert result.transaction.status == TransactionStatus.AUTHORIZED
        assert result.transaction.version == 1
        assert result.network_reference == "net_abc_123"
        assert result.authorization_code == "AUTH999"
        assert result.decline_code is None
        assert port.call_count == 1
        assert port.last_request is not None
        assert port.last_request.transaction_id == cmd.transaction_id
        assert port.last_request.merchant_id == cmd.merchant_id
        assert port.last_request.payment_token == cmd.payment_token
        assert port.last_request.amount == cmd.amount
        assert port.last_request.currency == cmd.currency
        assert port.last_request.correlation_id == cmd.correlation_id

    def test_declined_outcome_transitions_to_declined(self) -> None:
        canned = PaymentNetworkDeclinedResult(decline_code="INSUFFICIENT_FUNDS")
        port = MockPaymentNetworkPort(outcome=canned)
        service = AuthorizeTransactionService(payment_network=port)
        cmd = make_command()

        result = asyncio.run(service.execute(cmd))

        assert result.transaction.status == TransactionStatus.DECLINED
        assert result.transaction.version == 1
        assert result.decline_code == "INSUFFICIENT_FUNDS"
        assert result.network_reference is None
        assert result.authorization_code is None
        assert port.call_count == 1

    def test_timeout_error_transitions_to_pending_reconciliation(self) -> None:
        port = MockPaymentNetworkPort(
            outcome=PaymentNetworkTimeoutError("Read timed out")
        )
        service = AuthorizeTransactionService(payment_network=port)
        cmd = make_command()

        result = asyncio.run(service.execute(cmd))

        assert result.transaction.status == TransactionStatus.PENDING_RECONCILIATION
        assert result.transaction.version == 1
        assert result.transaction.pending_operation == PendingOperation.AUTHORIZATION
        assert result.network_reference is None
        assert result.authorization_code is None
        assert port.call_count == 1

    def test_http_408_timeout_transitions_to_pending_reconciliation(self) -> None:
        port = MockPaymentNetworkPort(
            outcome=PaymentNetworkTimeoutError("Payment network returned HTTP 408 Request Timeout.")
        )
        service = AuthorizeTransactionService(payment_network=port)
        cmd = make_command()

        result = asyncio.run(service.execute(cmd))

        assert result.transaction.status == TransactionStatus.PENDING_RECONCILIATION
        assert result.transaction.version == 1
        assert result.transaction.pending_operation == PendingOperation.AUTHORIZATION
        assert port.call_count == 1


    def test_invalid_response_error_transitions_to_pending_reconciliation(self) -> None:
        port = MockPaymentNetworkPort(
            outcome=PaymentNetworkInvalidResponseError("Malformed payload")
        )
        service = AuthorizeTransactionService(payment_network=port)
        cmd = make_command()

        result = asyncio.run(service.execute(cmd))

        assert result.transaction.status == TransactionStatus.PENDING_RECONCILIATION
        assert result.transaction.version == 1
        assert result.transaction.pending_operation == PendingOperation.AUTHORIZATION
        assert port.call_count == 1

    def test_unavailable_error_transitions_to_pending_reconciliation(self) -> None:
        port = MockPaymentNetworkPort(
            outcome=PaymentNetworkUnavailableError("Transport disconnected")
        )
        service = AuthorizeTransactionService(payment_network=port)
        cmd = make_command()

        result = asyncio.run(service.execute(cmd))

        assert result.transaction.status == TransactionStatus.PENDING_RECONCILIATION
        assert result.transaction.version == 1
        assert result.transaction.pending_operation == PendingOperation.AUTHORIZATION
        assert port.call_count == 1

    def test_rate_limit_error_transitions_to_failed_and_raises(self) -> None:
        port = MockPaymentNetworkPort(
            outcome=PaymentNetworkRateLimitError(retry_after=5)
        )
        service = AuthorizeTransactionService(payment_network=port)
        cmd = make_command()

        with pytest.raises(DownstreamRateLimitExceededError) as exc_info:
            asyncio.run(service.execute(cmd))

        assert exc_info.value.transaction.status == TransactionStatus.FAILED
        assert exc_info.value.transaction.version == 1
        assert exc_info.value.retry_after == 5
        assert port.call_count == 1

    def test_rejected_error_transitions_to_failed_and_raises(self) -> None:
        port = MockPaymentNetworkPort(
            outcome=PaymentNetworkRejectedError("Provider rejected request")
        )
        service = AuthorizeTransactionService(payment_network=port)
        cmd = make_command()

        with pytest.raises(DownstreamRejectedError) as exc_info:
            asyncio.run(service.execute(cmd))

        assert exc_info.value.transaction.status == TransactionStatus.FAILED
        assert exc_info.value.transaction.version == 1
        assert port.call_count == 1

    def test_no_automatic_retries_on_any_failure(self) -> None:
        """Invariants: the orchestration service never retries a failed or timed-out call."""
        failures = [
            PaymentNetworkTimeoutError("Timeout"),
            PaymentNetworkInvalidResponseError("Invalid JSON"),
            PaymentNetworkUnavailableError("Network drop"),
            PaymentNetworkRateLimitError(retry_after=1),
            PaymentNetworkRejectedError("Rejected"),
        ]

        for failure in failures:
            port = MockPaymentNetworkPort(outcome=failure)
            service = AuthorizeTransactionService(payment_network=port)
            cmd = make_command()

            try:
                asyncio.run(service.execute(cmd))
            except (DownstreamRateLimitExceededError, DownstreamRejectedError):
                pass

            assert (
                port.call_count == 1
            ), f"Expected 1 call for {failure}, got {port.call_count}"

    def test_sensitive_tokens_not_leaked_in_exceptions(self) -> None:
        port_rate = MockPaymentNetworkPort(outcome=PaymentNetworkRateLimitError(retry_after=1))
        service_rate = AuthorizeTransactionService(payment_network=port_rate)
        secret_token = "tok_test_sensitive_secret_token"
        cmd = make_command(payment_token=secret_token)

        with pytest.raises(DownstreamRateLimitExceededError) as exc_rate:
            asyncio.run(service_rate.execute(cmd))
        assert secret_token not in str(exc_rate.value)

        port_rej = MockPaymentNetworkPort(outcome=PaymentNetworkRejectedError("rejected"))
        service_rej = AuthorizeTransactionService(payment_network=port_rej)
        with pytest.raises(DownstreamRejectedError) as exc_rej:
            asyncio.run(service_rej.execute(cmd))
        assert secret_token not in str(exc_rej.value)
