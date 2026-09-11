"""Transaction domain module exposing entity, enums, and typed exceptions."""

from transaction_platform.domain.exceptions import (
    DomainException,
    InvalidReconciliationOperationError,
    InvalidReconciliationResolutionError,
    InvalidStateTransitionError,
    InvalidTransactionInvariantError,
    ReconciliationStateError,
)
from transaction_platform.domain.transaction import (
    PendingOperation,
    Transaction,
    TransactionStatus,
)

__all__ = [
    "DomainException",
    "InvalidTransactionInvariantError",
    "InvalidStateTransitionError",
    "InvalidReconciliationOperationError",
    "InvalidReconciliationResolutionError",
    "ReconciliationStateError",
    "TransactionStatus",
    "PendingOperation",
    "Transaction",
]
