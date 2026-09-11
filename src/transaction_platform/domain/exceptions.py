"""Typed domain exceptions for the transaction lifecycle and invariants."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from transaction_platform.domain.transaction import PendingOperation, TransactionStatus


class DomainException(Exception):
    """Base exception for all domain-level transaction errors."""
    pass


class InvalidTransactionInvariantError(DomainException):
    """Raised when an object creation or domain structural invariant is violated."""
    pass


class InvalidStateTransitionError(DomainException):
    """Raised when an ordinary lifecycle state transition violates domain transition rules."""

    def __init__(
        self,
        current_status: "TransactionStatus",
        attempted_status: "TransactionStatus",
        transaction_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.current_status = current_status
        self.attempted_status = attempted_status
        self.transaction_id = transaction_id
        self.reason = reason

        tx_context = f" for transaction '{transaction_id}'" if transaction_id else ""
        detail = f" ({reason})" if reason else ""
        super().__init__(
            f"Invalid transaction transition{tx_context}: "
            f"cannot transition from '{current_status.value}' to "
            f"'{attempted_status.value}'{detail}."
        )


class InvalidReconciliationOperationError(DomainException):
    """Raised when initiating reconciliation with an operation mismatched to current state."""

    def __init__(
        self,
        current_status: "TransactionStatus",
        attempted_operation: "PendingOperation",
        transaction_id: str | None = None,
    ) -> None:
        self.current_status = current_status
        self.attempted_operation = attempted_operation
        self.transaction_id = transaction_id

        tx_context = f" for transaction '{transaction_id}'" if transaction_id else ""
        super().__init__(
            f"Invalid reconciliation operation{tx_context}: "
            f"operation '{attempted_operation.value}' cannot be initiated "
            f"from state '{current_status.value}'."
        )


class InvalidReconciliationResolutionError(DomainException):
    """Raised when resolving reconciliation with a status invalid for the pending operation."""

    def __init__(
        self,
        pending_operation: "PendingOperation",
        attempted_status: "TransactionStatus",
        transaction_id: str | None = None,
    ) -> None:
        self.pending_operation = pending_operation
        self.attempted_status = attempted_status
        self.transaction_id = transaction_id

        tx_context = f" for transaction '{transaction_id}'" if transaction_id else ""
        super().__init__(
            f"Invalid reconciliation resolution{tx_context}: "
            f"cannot resolve pending operation '{pending_operation.value}' "
            f"to state '{attempted_status.value}'."
        )


class ReconciliationStateError(DomainException):
    """Raised when a reconciliation action is attempted in an inappropriate lifecycle state."""
    pass
