"""Unit tests for IdempotentTransactionOrchestrator."""

import asyncio
from typing import Any

from transaction_platform.application.authorize_transaction import (
    AuthorizeTransactionCommand,
    AuthorizeTransactionResult,
    AuthorizeTransactionService,
    DownstreamRateLimitExceededError,
    DownstreamRejectedError,
)
from transaction_platform.application.idempotent_orchestrator import (
    IdempotentTransactionOrchestrator,
)
from transaction_platform.domain.transaction import (
    Transaction,
    TransactionStatus,
)
from transaction_platform.ports.idempotency_coordinator import (
    IdempotencyCoordinatorPort,
    IdempotencyReservation,
    ReservationStatus,
    ResponseEnvelope,
)
from transaction_platform.ports.payment_network import (
    PaymentNetworkAuthorizationRequest,
    PaymentNetworkAuthorizedResult,
    PaymentNetworkDeclinedResult,
    PaymentNetworkPort,
)


class MockCoordinator(IdempotencyCoordinatorPort):
    """Test fake for IdempotencyCoordinatorPort capturing calls and returning canned
    reservations."""

    def __init__(self, reservation: IdempotencyReservation) -> None:
        self.reservation = reservation
        self.reserve_called = 0
        self.complete_called = 0
        self.last_completed_envelope: ResponseEnvelope | None = None
        self.last_completed_transaction: Transaction | None = None
        self.last_expected_version: int | None = None

    async def reserve(
        self,
        key_hash: str,
        request_fingerprint: str,
        transaction_id: str,
        merchant_id: str,
        amount: int,
        currency: str,
    ) -> IdempotencyReservation:
        self.reserve_called += 1
        return self.reservation

    async def complete(
        self,
        key_hash: str,
        transaction: Transaction,
        expected_version: int,
        envelope: ResponseEnvelope,
        network_reference: str | None = None,
        authorization_code: str | None = None,
        decline_code: str | None = None,
    ) -> None:
        self.complete_called += 1
        self.last_completed_transaction = transaction
        self.last_expected_version = expected_version
        self.last_completed_envelope = envelope


class MockPort(PaymentNetworkPort):
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.call_count = 0

    async def authorize(
        self, request: PaymentNetworkAuthorizationRequest
    ) -> PaymentNetworkAuthorizedResult | PaymentNetworkDeclinedResult:
        self.call_count += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome  # type: ignore[no-any-return]


def make_command() -> AuthorizeTransactionCommand:
    return AuthorizeTransactionCommand(
        transaction_id="txn_test_100",
        merchant_id="merchant_101",
        payment_token="tok_test_approved",
        amount=2500,
        currency="USD",
        correlation_id="corr_test_100",
    )


class TestIdempotentOrchestrator:
    """Verify orchestration boundary: reservation, downstream call, and atomic completion."""

    def test_acquired_authorized_completes_with_201_envelope(self) -> None:
        port = MockPort(
            outcome=PaymentNetworkAuthorizedResult(
                network_reference="net_ref_999",
                authorization_code="AUTH777",
            )
        )
        service = AuthorizeTransactionService(payment_network=port)
        coordinator = MockCoordinator(
            reservation=IdempotencyReservation(status=ReservationStatus.ACQUIRED)
        )
        orchestrator = IdempotentTransactionOrchestrator(coordinator=coordinator, service=service)
        command = make_command()

        envelope = asyncio.run(
            orchestrator.handle_authorization(command, idempotency_key="test_key_1")
        )

        assert envelope.status_code == 201
        assert envelope.body["status"] == "AUTHORIZED"
        assert envelope.body["version"] == 1
        assert envelope.body["network_reference"] == "net_ref_999"
        assert envelope.body["authorization_code"] == "AUTH777"
        assert coordinator.reserve_called == 1
        assert coordinator.complete_called == 1
        assert coordinator.last_expected_version == 0
        assert coordinator.last_completed_transaction is not None
        assert coordinator.last_completed_transaction.status == TransactionStatus.AUTHORIZED
        assert coordinator.last_completed_transaction.version == 1

    def test_acquired_declined_completes_with_201_envelope(self) -> None:
        port = MockPort(outcome=PaymentNetworkDeclinedResult(decline_code="DO_NOT_HONOR"))
        service = AuthorizeTransactionService(payment_network=port)
        coordinator = MockCoordinator(
            reservation=IdempotencyReservation(status=ReservationStatus.ACQUIRED)
        )
        orchestrator = IdempotentTransactionOrchestrator(coordinator=coordinator, service=service)
        command = make_command()

        envelope = asyncio.run(
            orchestrator.handle_authorization(command, idempotency_key="test_key_2")
        )

        assert envelope.status_code == 201
        assert envelope.body["status"] == "DECLINED"
        assert envelope.body["version"] == 1
        assert envelope.body["decline_code"] == "DO_NOT_HONOR"
        assert coordinator.complete_called == 1

    def test_acquired_rate_limited_completes_with_503_and_retry_after(self) -> None:
        txn = Transaction(transaction_id="txn_test_100")
        txn.transition_to(TransactionStatus.FAILED)
        service = AuthorizeTransactionService(payment_network=MockPort(outcome=None))
        # Override service.execute to raise DownstreamRateLimitExceededError
        async def mock_execute(cmd: Any) -> AuthorizeTransactionResult:
            raise DownstreamRateLimitExceededError(transaction=txn, retry_after=7)

        service.execute = mock_execute  # type: ignore[assignment,method-assign]
        coordinator = MockCoordinator(
            reservation=IdempotencyReservation(status=ReservationStatus.ACQUIRED)
        )
        orchestrator = IdempotentTransactionOrchestrator(coordinator=coordinator, service=service)
        command = make_command()

        envelope = asyncio.run(
            orchestrator.handle_authorization(command, idempotency_key="test_key_3")
        )

        assert envelope.status_code == 503
        assert envelope.headers == {"Retry-After": "7"}
        assert envelope.body["type"] == "urn:problem-type:provider-rate-limited"
        assert coordinator.complete_called == 1
        assert coordinator.last_completed_transaction is not None
        assert coordinator.last_completed_transaction.status == TransactionStatus.FAILED

    def test_acquired_rejected_completes_with_502_problem(self) -> None:
        txn = Transaction(transaction_id="txn_test_100")
        txn.transition_to(TransactionStatus.FAILED)
        service = AuthorizeTransactionService(payment_network=MockPort(outcome=None))
        async def mock_execute(cmd: Any) -> AuthorizeTransactionResult:
            raise DownstreamRejectedError(transaction=txn)

        service.execute = mock_execute  # type: ignore[assignment,method-assign]
        coordinator = MockCoordinator(
            reservation=IdempotencyReservation(status=ReservationStatus.ACQUIRED)
        )
        orchestrator = IdempotentTransactionOrchestrator(coordinator=coordinator, service=service)
        command = make_command()

        envelope = asyncio.run(
            orchestrator.handle_authorization(command, idempotency_key="test_key_4")
        )

        assert envelope.status_code == 502
        assert envelope.body["type"] == "urn:problem-type:downstream-rejected"
        assert coordinator.complete_called == 1

    def test_completed_reservation_replays_cached_envelope(self) -> None:
        cached = ResponseEnvelope(
            status_code=201,
            body={"transaction_id": "txn_test_100", "status": "AUTHORIZED", "version": 1},
            is_replay=True,
        )
        port = MockPort(outcome=None)
        service = AuthorizeTransactionService(payment_network=port)
        coordinator = MockCoordinator(
            reservation=IdempotencyReservation(
                status=ReservationStatus.COMPLETED,
                stored_envelope=cached,
            )
        )
        orchestrator = IdempotentTransactionOrchestrator(coordinator=coordinator, service=service)
        command = make_command()

        envelope = asyncio.run(
            orchestrator.handle_authorization(command, idempotency_key="test_key_5")
        )

        assert envelope.status_code == 201
        assert envelope.is_replay is True
        assert port.call_count == 0
        assert coordinator.complete_called == 0

    def test_in_progress_reservation_returns_409_problem(self) -> None:
        port = MockPort(outcome=None)
        service = AuthorizeTransactionService(payment_network=port)
        coordinator = MockCoordinator(
            reservation=IdempotencyReservation(status=ReservationStatus.IN_PROGRESS)
        )
        orchestrator = IdempotentTransactionOrchestrator(coordinator=coordinator, service=service)
        command = make_command()

        envelope = asyncio.run(
            orchestrator.handle_authorization(command, idempotency_key="test_key_6")
        )

        assert envelope.status_code == 409
        assert envelope.headers == {"Retry-After": "1"}
        assert envelope.body["type"] == "urn:problem-type:idempotency-in-progress"
        assert port.call_count == 0
        assert coordinator.complete_called == 0

    def test_payload_mismatch_returns_idempotency_conflict_409(self) -> None:
        port = MockPort(outcome=None)
        service = AuthorizeTransactionService(payment_network=port)
        coordinator = MockCoordinator(
            reservation=IdempotencyReservation(status=ReservationStatus.IDEMPOTENCY_CONFLICT)
        )
        orchestrator = IdempotentTransactionOrchestrator(coordinator=coordinator, service=service)
        command = make_command()

        envelope = asyncio.run(
            orchestrator.handle_authorization(command, idempotency_key="test_key_7")
        )

        assert envelope.status_code == 409
        assert envelope.body["type"] == "urn:problem-type:idempotency-conflict"
        assert port.call_count == 0
        assert coordinator.complete_called == 0

    def test_transaction_id_conflict_returns_409(self) -> None:
        port = MockPort(outcome=None)
        service = AuthorizeTransactionService(payment_network=port)
        coordinator = MockCoordinator(
            reservation=IdempotencyReservation(status=ReservationStatus.TRANSACTION_ID_CONFLICT)
        )
        orchestrator = IdempotentTransactionOrchestrator(coordinator=coordinator, service=service)
        command = make_command()

        envelope = asyncio.run(
            orchestrator.handle_authorization(command, idempotency_key="test_key_8")
        )

        assert envelope.status_code == 409
        assert envelope.body["type"] == "urn:problem-type:transaction-id-conflict"
        assert port.call_count == 0
        assert coordinator.complete_called == 0
