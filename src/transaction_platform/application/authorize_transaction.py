"""Application service for orchestrating transaction authorizations.

Note on Persistence and In-Memory Lifecycle:
Transactions in this milestone are strictly request-scoped and in-memory.
They are not persisted to a database or cache. Pending transactions cannot
actually be reconciled out-of-band until durable storage and the background
reconciliation worker are introduced in subsequent milestones.
"""

from dataclasses import dataclass

from transaction_platform.domain.transaction import (
    PendingOperation,
    Transaction,
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


@dataclass(frozen=True)
class AuthorizeTransactionCommand:
    """Input command to authorize a payment transaction."""

    transaction_id: str
    merchant_id: str
    payment_token: str
    amount: int
    currency: str
    correlation_id: str


@dataclass(frozen=True)
class AuthorizeTransactionResult:
    """Result of an authorization orchestration execution."""

    transaction: Transaction
    network_reference: str | None = None
    authorization_code: str | None = None
    decline_code: str | None = None


class DownstreamRateLimitExceededError(Exception):
    """Raised when downstream rate limiting causes transaction to transition to FAILED."""

    def __init__(self, transaction: Transaction, retry_after: int = 1) -> None:
        super().__init__("Downstream payment network rate limit exceeded.")
        self.transaction = transaction
        self.retry_after = retry_after


class DownstreamRejectedError(Exception):
    """Raised when downstream request rejection causes transaction to transition to FAILED."""

    def __init__(self, transaction: Transaction) -> None:
        super().__init__("Downstream payment network rejected authorization request.")
        self.transaction = transaction


class AuthorizeTransactionService:
    """Orchestrates transaction authorization against the downstream payment network."""

    def __init__(self, payment_network: PaymentNetworkPort) -> None:
        self.payment_network = payment_network

    async def execute(self, command: AuthorizeTransactionCommand) -> AuthorizeTransactionResult:
        """Execute the authorization lifecycle for a single transaction.

        Transitions:
        - Authorized -> AUTHORIZED
        - Declined -> DECLINED
        - Timeout -> PENDING_RECONCILIATION
        - Malformed HTTP 200 -> PENDING_RECONCILIATION
        - Transport unavailable / 5xx -> PENDING_RECONCILIATION
        - Explicit HTTP 429 -> FAILED (raises DownstreamRateLimitExceededError)
        - Explicit HTTP 400/422/4xx -> FAILED (raises DownstreamRejectedError)
        """
        transaction = Transaction(transaction_id=command.transaction_id)

        port_request = PaymentNetworkAuthorizationRequest(
            transaction_id=command.transaction_id,
            merchant_id=command.merchant_id,
            payment_token=command.payment_token,
            amount=command.amount,
            currency=command.currency,
            correlation_id=command.correlation_id,
        )

        try:
            result = await self.payment_network.authorize(port_request)
        except PaymentNetworkTimeoutError:
            transaction.mark_pending_reconciliation(PendingOperation.AUTHORIZATION)
            return AuthorizeTransactionResult(transaction=transaction)
        except PaymentNetworkInvalidResponseError:
            transaction.mark_pending_reconciliation(PendingOperation.AUTHORIZATION)
            return AuthorizeTransactionResult(transaction=transaction)
        except PaymentNetworkUnavailableError:
            transaction.mark_pending_reconciliation(PendingOperation.AUTHORIZATION)
            return AuthorizeTransactionResult(transaction=transaction)
        except PaymentNetworkRateLimitError as exc:
            # Note: Under the emulator contract, HTTP 429 guarantees the request was rejected
            # before authorization processing began. This assumption is specific to the emulator.
            transaction.transition_to(TransactionStatus.FAILED)
            raise DownstreamRateLimitExceededError(
                transaction=transaction, retry_after=exc.retry_after
            ) from exc
        except PaymentNetworkRejectedError as exc:
            transaction.transition_to(TransactionStatus.FAILED)
            raise DownstreamRejectedError(transaction=transaction) from exc

        if isinstance(result, PaymentNetworkAuthorizedResult):
            # The payment network confirmed the authorization succeeded
            transaction.transition_to(TransactionStatus.AUTHORIZED)
            return AuthorizeTransactionResult(
                transaction=transaction,
                network_reference=result.network_reference,
                authorization_code=result.authorization_code,
            )

        if isinstance(result, PaymentNetworkDeclinedResult):
            transaction.transition_to(TransactionStatus.DECLINED)
            return AuthorizeTransactionResult(
                transaction=transaction,
                decline_code=result.decline_code,
            )

        # Defensive fallback for unrecognized return types
        transaction.mark_pending_reconciliation(PendingOperation.AUTHORIZATION)
        return AuthorizeTransactionResult(transaction=transaction)
