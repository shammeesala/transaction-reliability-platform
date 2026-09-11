"""Framework-independent transaction domain entity and lifecycle state machine.

Standard library only: encapsulates transaction state, version tracking,
ordinary transitions, and ambiguous-outcome reconciliation invariants.

Note on Concurrency and Atomicity:
This in-memory entity does not provide thread safety, database transaction
atomicity, or distributed concurrency control. Instead, it enforces domain
invariants sequentially: all validation checks must succeed before mutating
internal fields. The `version` field is incremented on every valid state
transition to prepare for persistence-level optimistic concurrency control
in a subsequent milestone.
"""

from enum import StrEnum

from transaction_platform.domain.exceptions import (
    InvalidReconciliationOperationError,
    InvalidReconciliationResolutionError,
    InvalidStateTransitionError,
    InvalidTransactionInvariantError,
    ReconciliationStateError,
)


class TransactionStatus(StrEnum):
    """Enumeration of all discrete transaction lifecycle states."""

    CREATED = "CREATED"
    AUTHORIZED = "AUTHORIZED"
    DECLINED = "DECLINED"
    CAPTURED = "CAPTURED"
    REFUNDED = "REFUNDED"
    PENDING_RECONCILIATION = "PENDING_RECONCILIATION"
    FAILED = "FAILED"


class PendingOperation(StrEnum):
    """Operation undergoing out-of-band inquiry while in PENDING_RECONCILIATION."""

    AUTHORIZATION = "AUTHORIZATION"
    CAPTURE = "CAPTURE"
    REFUND = "REFUND"


# Private transition definitions to encapsulate domain logic internally
_TERMINAL_STATES: frozenset[TransactionStatus] = frozenset({
    TransactionStatus.DECLINED,
    TransactionStatus.FAILED,
    TransactionStatus.REFUNDED,
})

_ORDINARY_TRANSITIONS: dict[TransactionStatus, frozenset[TransactionStatus]] = {
    TransactionStatus.CREATED: frozenset({
        TransactionStatus.AUTHORIZED,
        TransactionStatus.DECLINED,
        TransactionStatus.FAILED,
    }),
    TransactionStatus.AUTHORIZED: frozenset({
        TransactionStatus.CAPTURED,
    }),
    TransactionStatus.CAPTURED: frozenset({
        TransactionStatus.REFUNDED,
    }),
    TransactionStatus.PENDING_RECONCILIATION: frozenset(),
    TransactionStatus.DECLINED: frozenset(),
    TransactionStatus.FAILED: frozenset(),
    TransactionStatus.REFUNDED: frozenset(),
}

_RECONCILIATION_ENTRIES: dict[TransactionStatus, PendingOperation] = {
    TransactionStatus.CREATED: PendingOperation.AUTHORIZATION,
    TransactionStatus.AUTHORIZED: PendingOperation.CAPTURE,
    TransactionStatus.CAPTURED: PendingOperation.REFUND,
}

_RECONCILIATION_RESOLUTIONS: dict[PendingOperation, frozenset[TransactionStatus]] = {
    # Pending AUTHORIZATION:
    # - AUTHORIZED: network confirmed the authorization succeeded
    # - DECLINED: network confirmed the card was declined
    # - FAILED: network confirmed that no authorization was created
    PendingOperation.AUTHORIZATION: frozenset({
        TransactionStatus.AUTHORIZED,
        TransactionStatus.DECLINED,
        TransactionStatus.FAILED,
    }),
    # Pending CAPTURE:
    # - CAPTURED: network confirmed capture succeeded
    # - AUTHORIZED: network confirmed capture did not occur; reverts to authorized
    PendingOperation.CAPTURE: frozenset({
        TransactionStatus.CAPTURED,
        TransactionStatus.AUTHORIZED,
    }),
    # Pending REFUND:
    # - REFUNDED: network confirmed refund succeeded
    # - CAPTURED: network confirmed refund did not occur; reverts to captured
    PendingOperation.REFUND: frozenset({
        TransactionStatus.REFUNDED,
        TransactionStatus.CAPTURED,
    }),
}


class Transaction:
    """
    Transaction domain entity enforcing lifecycle transitions and version tracking.
    """

    def __init__(
        self,
        transaction_id: str,
        status: TransactionStatus = TransactionStatus.CREATED,
        version: int = 0,
        pending_operation: PendingOperation | None = None,
    ) -> None:
        self._validate_initial_invariants(transaction_id, status, version, pending_operation)

        self._transaction_id = transaction_id
        self._status = status
        self._version = version
        self._pending_operation = pending_operation

    @staticmethod
    def _validate_initial_invariants(
        transaction_id: str,
        status: TransactionStatus,
        version: int,
        pending_operation: PendingOperation | None,
    ) -> None:
        # Reject non-string, empty, or whitespace-only IDs
        if type(transaction_id) is not str or not transaction_id.strip():
            raise InvalidTransactionInvariantError("transaction_id must be a non-empty string.")

        # Reject non-integers, booleans (bool is a subclass of int in Python), and negative numbers
        if type(version) is not int or version < 0:
            raise InvalidTransactionInvariantError(
                f"version must be an exact integer >= 0, got {type(version).__name__} "
                f"({version!r})."
            )

        if not isinstance(status, TransactionStatus):
            raise InvalidTransactionInvariantError(
                f"status must be a valid TransactionStatus enum, got {type(status).__name__} "
                f"({status!r})."
            )

        if status == TransactionStatus.PENDING_RECONCILIATION:
            if pending_operation is None:
                raise InvalidTransactionInvariantError(
                    "pending_operation must not be None when status is PENDING_RECONCILIATION."
                )
            if not isinstance(pending_operation, PendingOperation):
                raise InvalidTransactionInvariantError(
                    f"pending_operation must be a valid PendingOperation enum, "
                    f"got {type(pending_operation).__name__} ({pending_operation!r})."
                )
        else:
            if pending_operation is not None:
                raise InvalidTransactionInvariantError(
                    f"pending_operation must be None when status is '{status.value}'."
                )

    @property
    def transaction_id(self) -> str:
        return self._transaction_id

    @property
    def status(self) -> TransactionStatus:
        return self._status

    @property
    def version(self) -> int:
        return self._version

    @property
    def pending_operation(self) -> PendingOperation | None:
        return self._pending_operation

    def is_terminal(self) -> bool:
        """Return True if the transaction has reached an irreversible end state."""
        return self._status in _TERMINAL_STATES

    def transition_to(self, target_status: TransactionStatus) -> None:
        """
        Execute an ordinary lifecycle state transition.

        Validates runtime type and transition rules before mutating internal fields.
        """
        if not isinstance(target_status, TransactionStatus):
            raise TypeError(
                f"target_status must be an instance of TransactionStatus, "
                f"got {type(target_status).__name__} ({target_status!r})."
            )

        if self._status == TransactionStatus.PENDING_RECONCILIATION:
            raise ReconciliationStateError(
                f"Transaction '{self._transaction_id}' is in PENDING_RECONCILIATION; "
                f"ordinary transition to '{target_status.value}' is prohibited. "
                f"Use resolve_reconciliation()."
            )

        if target_status == TransactionStatus.PENDING_RECONCILIATION:
            raise InvalidStateTransitionError(
                current_status=self._status,
                attempted_status=target_status,
                transaction_id=self._transaction_id,
                reason=(
                    "Cannot transition directly to PENDING_RECONCILIATION; "
                    "use mark_pending_reconciliation()"
                ),
            )

        allowed = _ORDINARY_TRANSITIONS.get(self._status, frozenset())
        if target_status not in allowed:
            raise InvalidStateTransitionError(
                current_status=self._status,
                attempted_status=target_status,
                transaction_id=self._transaction_id,
            )

        # All validation complete; update state and advance version counter
        self._status = target_status
        self._version += 1

    def mark_pending_reconciliation(self, operation: PendingOperation) -> None:
        """
        Transition to PENDING_RECONCILIATION recording the in-flight ambiguous operation.

        Validates runtime type and transition rules before mutating internal fields.
        """
        if not isinstance(operation, PendingOperation):
            raise TypeError(
                f"operation must be an instance of PendingOperation, "
                f"got {type(operation).__name__} ({operation!r})."
            )

        if self._status == TransactionStatus.PENDING_RECONCILIATION:
            raise ReconciliationStateError(
                f"Transaction '{self._transaction_id}' is already in PENDING_RECONCILIATION."
            )

        expected_operation = _RECONCILIATION_ENTRIES.get(self._status)
        if expected_operation is None or expected_operation != operation:
            raise InvalidReconciliationOperationError(
                current_status=self._status,
                attempted_operation=operation,
                transaction_id=self._transaction_id,
            )

        # All validation complete; update state and advance version counter
        self._status = TransactionStatus.PENDING_RECONCILIATION
        self._pending_operation = operation
        self._version += 1

    def resolve_reconciliation(self, target_status: TransactionStatus) -> None:
        """
        Resolve an ambiguous outcome back to a valid stable state.

        Validates runtime type and resolution rules before mutating internal fields.
        """
        if not isinstance(target_status, TransactionStatus):
            raise TypeError(
                f"target_status must be an instance of TransactionStatus, "
                f"got {type(target_status).__name__} ({target_status!r})."
            )

        if self._status != TransactionStatus.PENDING_RECONCILIATION:
            raise ReconciliationStateError(
                f"Cannot resolve reconciliation for transaction '{self._transaction_id}': "
                f"current status is '{self._status.value}', expected 'PENDING_RECONCILIATION'."
            )

        if self._pending_operation is None:
            raise InvalidTransactionInvariantError(
                f"Transaction '{self._transaction_id}' corrupted: "
                f"in PENDING_RECONCILIATION without pending_operation."
            )

        allowed_resolutions = _RECONCILIATION_RESOLUTIONS.get(
            self._pending_operation, frozenset()
        )
        if target_status not in allowed_resolutions:
            raise InvalidReconciliationResolutionError(
                pending_operation=self._pending_operation,
                attempted_status=target_status,
                transaction_id=self._transaction_id,
            )

        # All validation complete; update state and advance version counter
        self._status = target_status
        self._pending_operation = None
        self._version += 1
