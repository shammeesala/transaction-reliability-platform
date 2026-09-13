"""Protocol and data structures for atomic idempotency reservation and completion."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from transaction_platform.domain.transaction import Transaction


class ReservationStatus(StrEnum):
    """Status of an idempotency reservation attempt."""

    ACQUIRED = "ACQUIRED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"        # Same key, different payload
    TRANSACTION_ID_CONFLICT = "TRANSACTION_ID_CONFLICT"  # Different key, same transaction_id


@dataclass(frozen=True)
class ResponseEnvelope:
    """Stable HTTP response representation for persistence, replay, and problem details."""

    status_code: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)
    is_replay: bool = False
    media_type: str | None = None


@dataclass(frozen=True)
class IdempotencyReservation:
    """Result of an atomic reservation attempt."""

    status: ReservationStatus
    stored_envelope: ResponseEnvelope | None = None


class PersistenceError(Exception):
    """Base exception for persistence-level database errors."""


class OptimisticConcurrencyError(PersistenceError):
    """Raised when an update fails because the version or state does not match expectations."""


class IdempotencyCoordinatorPort(Protocol):
    """Unit-of-work boundary for atomic reservation and completion across tables."""

    async def reserve(
        self,
        key_hash: str,
        request_fingerprint: str,
        transaction_id: str,
        merchant_id: str,
        amount: int,
        currency: str,
    ) -> IdempotencyReservation:
        """Atomically insert CREATED transaction (v=0) and IN_PROGRESS idempotency record."""
        ...

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
        """Atomically update transaction (optimistic lock) and mark idempotency COMPLETED."""
        ...
