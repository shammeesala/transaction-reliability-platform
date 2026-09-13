"""Application orchestrator coordinating idempotency, execution, and completion."""

from transaction_platform.application.authorize_transaction import (
    AuthorizeTransactionCommand,
    AuthorizeTransactionResult,
    AuthorizeTransactionService,
    DownstreamRateLimitExceededError,
    DownstreamRejectedError,
)
from transaction_platform.common.idempotency import (
    compute_key_hash,
    compute_request_fingerprint,
)
from transaction_platform.common.problem_details import (
    PROBLEM_MEDIA_TYPE,
    ProblemDetail,
)
from transaction_platform.domain.transaction import TransactionStatus
from transaction_platform.ports.idempotency_coordinator import (
    IdempotencyCoordinatorPort,
    ReservationStatus,
    ResponseEnvelope,
)


def _make_problem_envelope(
    status_code: int,
    problem_type: str,
    title: str,
    detail: str,
    headers: dict[str, str] | None = None,
) -> ResponseEnvelope:
    """Create a ResponseEnvelope containing an RFC 7807 problem details document."""
    problem = ProblemDetail(
        type=problem_type,
        title=title,
        status=status_code,
        detail=detail,
    )
    return ResponseEnvelope(
        status_code=status_code,
        body=problem.model_dump(exclude_none=True),
        headers=headers or {},
        media_type=PROBLEM_MEDIA_TYPE,
    )


class IdempotentTransactionOrchestrator:
    """Orchestrates reservation, payment-network invocation, response mapping, and completion."""

    def __init__(
        self,
        coordinator: IdempotencyCoordinatorPort,
        service: AuthorizeTransactionService,
    ) -> None:
        self.coordinator = coordinator
        self.service = service

    def map_result_to_envelope(self, result: AuthorizeTransactionResult) -> ResponseEnvelope:
        """Map a successful or ambiguous AuthorizeTransactionResult to a ResponseEnvelope."""
        txn = result.transaction
        if txn.status == TransactionStatus.AUTHORIZED:
            return ResponseEnvelope(
                status_code=201,
                body={
                    "transaction_id": txn.transaction_id,
                    "status": "AUTHORIZED",
                    "version": txn.version,
                    "network_reference": result.network_reference or "",
                    "authorization_code": result.authorization_code or "",
                },
                media_type="application/json",
            )

        if txn.status == TransactionStatus.DECLINED:
            return ResponseEnvelope(
                status_code=201,
                body={
                    "transaction_id": txn.transaction_id,
                    "status": "DECLINED",
                    "version": txn.version,
                    "decline_code": result.decline_code or "",
                },
                media_type="application/json",
            )

        return ResponseEnvelope(
            status_code=202,
            body={
                "transaction_id": txn.transaction_id,
                "status": "PENDING_RECONCILIATION",
                "version": txn.version,
                "pending_operation": "AUTHORIZATION",
            },
            media_type="application/json",
        )

    async def handle_authorization(
        self,
        command: AuthorizeTransactionCommand,
        idempotency_key: str,
    ) -> ResponseEnvelope:
        """Handle inbound payment authorization with atomic idempotency guarantees."""
        key_hash = compute_key_hash(idempotency_key)
        request_fingerprint = compute_request_fingerprint(
            amount=command.amount,
            currency=command.currency,
            merchant_id=command.merchant_id,
            payment_token=command.payment_token,
            transaction_id=command.transaction_id,
        )

        reservation = await self.coordinator.reserve(
            key_hash=key_hash,
            request_fingerprint=request_fingerprint,
            transaction_id=command.transaction_id,
            merchant_id=command.merchant_id,
            amount=command.amount,
            currency=command.currency,
        )

        if reservation.status == ReservationStatus.COMPLETED:
            assert reservation.stored_envelope is not None
            return reservation.stored_envelope

        if reservation.status == ReservationStatus.IDEMPOTENCY_CONFLICT:
            return _make_problem_envelope(
                status_code=409,
                problem_type="urn:problem-type:idempotency-conflict",
                title="Idempotency Conflict",
                detail=(
                    "The request payload does not match the original request "
                    "for this idempotency key."
                ),
            )

        if reservation.status == ReservationStatus.IN_PROGRESS:
            return _make_problem_envelope(
                status_code=409,
                problem_type="urn:problem-type:idempotency-in-progress",
                title="Idempotency Request In Progress",
                detail="A request with this idempotency key is currently being processed.",
                headers={"Retry-After": "1"},
            )

        if reservation.status == ReservationStatus.TRANSACTION_ID_CONFLICT:
            return _make_problem_envelope(
                status_code=409,
                problem_type="urn:problem-type:transaction-id-conflict",
                title="Transaction ID Conflict",
                detail=(
                    "The provided transaction_id already exists under a different idempotency key."
                ),
            )

        # ReservationStatus.ACQUIRED: proceed to downstream invocation
        network_reference: str | None = None
        authorization_code: str | None = None
        decline_code: str | None = None

        try:
            result = await self.service.execute(command)
            txn = result.transaction
            network_reference = result.network_reference
            authorization_code = result.authorization_code
            decline_code = result.decline_code
            envelope = self.map_result_to_envelope(result)

        except DownstreamRateLimitExceededError as exc:
            txn = exc.transaction
            envelope = _make_problem_envelope(
                status_code=503,
                problem_type="urn:problem-type:provider-rate-limited",
                title="Downstream rate limit exceeded",
                detail="Downstream payment network rate limit exceeded.",
                headers={"Retry-After": str(exc.retry_after)},
            )

        except DownstreamRejectedError as exc:
            txn = exc.transaction
            envelope = _make_problem_envelope(
                status_code=502,
                problem_type="urn:problem-type:downstream-rejected",
                title="Payment network request rejected",
                detail="The payment network rejected the authorization request.",
            )

        # Atomically complete transaction state and cache response envelope
        # Note: Initial reservation has version 0, so expected_version is 0.
        await self.coordinator.complete(
            key_hash=key_hash,
            transaction=txn,
            expected_version=0,
            envelope=envelope,
            network_reference=network_reference,
            authorization_code=authorization_code,
            decline_code=decline_code,
        )

        return envelope
