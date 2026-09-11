"""Unit tests for the framework-independent transaction state machine."""

import itertools
from typing import Any

import pytest

from transaction_platform.domain.exceptions import (
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

# ---------------------------------------------------------------------------
# 1. Construction & Invariant Tests
# ---------------------------------------------------------------------------


def test_new_created_transaction_defaults() -> None:
    """Verify clean instantiation of a new transaction with version 0."""
    tx = Transaction(transaction_id="tx_001")
    assert tx.transaction_id == "tx_001"
    assert tx.status == TransactionStatus.CREATED
    assert tx.version == 0
    assert tx.pending_operation is None
    assert tx.is_terminal() is False


def test_rehydrated_stable_transaction() -> None:
    """Verify instantiating a previously persisted stable transaction with positive version."""
    tx = Transaction(
        transaction_id="tx_002",
        status=TransactionStatus.AUTHORIZED,
        version=5,
        pending_operation=None,
    )
    assert tx.status == TransactionStatus.AUTHORIZED
    assert tx.version == 5
    assert tx.pending_operation is None


def test_rehydrated_pending_reconciliation_transaction() -> None:
    """Verify instantiating a persisted transaction undergoing reconciliation."""
    tx = Transaction(
        transaction_id="tx_003",
        status=TransactionStatus.PENDING_RECONCILIATION,
        version=2,
        pending_operation=PendingOperation.AUTHORIZATION,
    )
    assert tx.status == TransactionStatus.PENDING_RECONCILIATION
    assert tx.version == 2
    assert tx.pending_operation == PendingOperation.AUTHORIZATION


@pytest.mark.parametrize("invalid_id", ["", "   ", "\t\n", None, 123])
def test_invalid_transaction_id_rejected(invalid_id: Any) -> None:
    """Verify empty, whitespace-only, or non-string transaction IDs are rejected."""
    with pytest.raises(InvalidTransactionInvariantError):
        Transaction(transaction_id=invalid_id)


@pytest.mark.parametrize("invalid_version", [True, False, 1.5, "1", -1, -5])
def test_invalid_version_types_and_values_rejected(invalid_version: Any) -> None:
    """Verify boolean versions, floats, strings, and negative integers are rejected."""
    with pytest.raises(InvalidTransactionInvariantError) as exc_info:
        Transaction(transaction_id="tx_v_err", version=invalid_version)
    assert "version must be an exact integer >= 0" in str(exc_info.value)


def test_pending_status_without_operation_rejected() -> None:
    """Verify PENDING_RECONCILIATION without pending_operation raises an error."""
    with pytest.raises(InvalidTransactionInvariantError) as exc_info:
        Transaction(
            transaction_id="tx_no_op",
            status=TransactionStatus.PENDING_RECONCILIATION,
            pending_operation=None,
        )
    assert "pending_operation must not be None" in str(exc_info.value)


@pytest.mark.parametrize(
    "stable_status",
    [
        TransactionStatus.CREATED,
        TransactionStatus.AUTHORIZED,
        TransactionStatus.CAPTURED,
        TransactionStatus.DECLINED,
        TransactionStatus.FAILED,
        TransactionStatus.REFUNDED,
    ],
)
def test_stable_status_with_unexpected_pending_op_rejected(
    stable_status: TransactionStatus,
) -> None:
    """Verify stable states cannot be instantiated with a pending operation."""
    with pytest.raises(InvalidTransactionInvariantError) as exc_info:
        Transaction(
            transaction_id="tx_unexpected_op",
            status=stable_status,
            pending_operation=PendingOperation.AUTHORIZATION,
        )
    assert "pending_operation must be None" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 2. Runtime Type Safety Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_type", ["AUTHORIZED", None, 123, 4.5, False, []])
def test_transition_to_invalid_runtime_type(bad_type: Any) -> None:
    """Verify passing non-TransactionStatus raises TypeError and preserves entity state."""
    tx = Transaction(transaction_id="tx_type_err", status=TransactionStatus.CREATED, version=1)

    with pytest.raises(TypeError) as exc_info:
        tx.transition_to(bad_type)

    assert "target_status must be an instance of TransactionStatus" in str(exc_info.value)
    assert tx.status == TransactionStatus.CREATED
    assert tx.version == 1
    assert tx.pending_operation is None


@pytest.mark.parametrize("bad_type", ["AUTHORIZATION", None, 123, True, {}])
def test_mark_pending_reconciliation_invalid_runtime_type(bad_type: Any) -> None:
    """Verify passing non-PendingOperation raises TypeError and preserves entity state."""
    tx = Transaction(transaction_id="tx_rec_type_err", status=TransactionStatus.CREATED, version=2)

    with pytest.raises(TypeError) as exc_info:
        tx.mark_pending_reconciliation(bad_type)

    assert "operation must be an instance of PendingOperation" in str(exc_info.value)
    assert tx.status == TransactionStatus.CREATED
    assert tx.version == 2
    assert tx.pending_operation is None


@pytest.mark.parametrize("bad_type", ["AUTHORIZED", None, 123, False])
def test_resolve_reconciliation_invalid_runtime_type(bad_type: Any) -> None:
    """Verify passing non-TransactionStatus to resolve raises TypeError and preserves state."""
    tx = Transaction(
        transaction_id="tx_res_type_err",
        status=TransactionStatus.PENDING_RECONCILIATION,
        version=3,
        pending_operation=PendingOperation.AUTHORIZATION,
    )

    with pytest.raises(TypeError) as exc_info:
        tx.resolve_reconciliation(bad_type)

    assert "target_status must be an instance of TransactionStatus" in str(exc_info.value)
    assert tx.status == TransactionStatus.PENDING_RECONCILIATION
    assert tx.version == 3
    assert tx.pending_operation == PendingOperation.AUTHORIZATION


# ---------------------------------------------------------------------------
# 3. Exhaustive Ordinary Transition Matrix Tests (49 Pairs)
# ---------------------------------------------------------------------------

APPROVED_ORDINARY_PAIRS = frozenset({
    (TransactionStatus.CREATED, TransactionStatus.AUTHORIZED),
    (TransactionStatus.CREATED, TransactionStatus.DECLINED),
    (TransactionStatus.CREATED, TransactionStatus.FAILED),
    (TransactionStatus.AUTHORIZED, TransactionStatus.CAPTURED),
    (TransactionStatus.CAPTURED, TransactionStatus.REFUNDED),
})

ALL_STATUS_PAIRS = list(itertools.product(TransactionStatus, TransactionStatus))


@pytest.mark.parametrize("source_status,target_status", ALL_STATUS_PAIRS)
def test_exhaustive_ordinary_transition_matrix(
    source_status: TransactionStatus, target_status: TransactionStatus
) -> None:
    """
    Exhaustively evaluate all 49 state pairs.

    Approved pairs must succeed and increment version by 1.
    All 44 unapproved pairs must raise domain exceptions and preserve state exactly.
    """
    initial_op = (
        PendingOperation.AUTHORIZATION
        if source_status == TransactionStatus.PENDING_RECONCILIATION
        else None
    )
    tx = Transaction(
        transaction_id="tx_matrix",
        status=source_status,
        version=10,
        pending_operation=initial_op,
    )

    if (source_status, target_status) in APPROVED_ORDINARY_PAIRS:
        tx.transition_to(target_status)
        assert tx.status == target_status
        assert tx.version == 11
        assert tx.pending_operation is None
    else:
        # Expected exception depends on source status
        if source_status == TransactionStatus.PENDING_RECONCILIATION:
            expected_exc = ReconciliationStateError
        else:
            expected_exc = InvalidStateTransitionError

        with pytest.raises(expected_exc):
            tx.transition_to(target_status)

        # Invariant: Status, version, and pending_operation must remain pristine
        assert tx.status == source_status
        assert tx.version == 10
        assert tx.pending_operation == initial_op


# ---------------------------------------------------------------------------
# 4. Reconciliation Entry and Resolution Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stable_status,op",
    [
        (TransactionStatus.CREATED, PendingOperation.AUTHORIZATION),
        (TransactionStatus.AUTHORIZED, PendingOperation.CAPTURE),
        (TransactionStatus.CAPTURED, PendingOperation.REFUND),
    ],
)
def test_valid_reconciliation_entry(
    stable_status: TransactionStatus, op: PendingOperation
) -> None:
    """Verify valid reconciliation entry updates status, records op, and increments version."""
    tx = Transaction(transaction_id="tx_rec_entry", status=stable_status, version=1)
    tx.mark_pending_reconciliation(op)

    assert tx.status == TransactionStatus.PENDING_RECONCILIATION
    assert tx.pending_operation == op
    assert tx.version == 2


@pytest.mark.parametrize(
    "op,target_resolution",
    [
        # Pending AUTHORIZATION
        (PendingOperation.AUTHORIZATION, TransactionStatus.AUTHORIZED),
        (PendingOperation.AUTHORIZATION, TransactionStatus.DECLINED),
        (PendingOperation.AUTHORIZATION, TransactionStatus.FAILED),
        # Pending CAPTURE
        (PendingOperation.CAPTURE, TransactionStatus.CAPTURED),
        # Reverts to AUTHORIZED when capture didn't occur
        (PendingOperation.CAPTURE, TransactionStatus.AUTHORIZED),
        # Pending REFUND
        (PendingOperation.REFUND, TransactionStatus.REFUNDED),
        # Reverts to CAPTURED when refund didn't occur
        (PendingOperation.REFUND, TransactionStatus.CAPTURED),
    ],
)
def test_valid_reconciliation_resolution(
    op: PendingOperation, target_resolution: TransactionStatus
) -> None:
    """Verify resolving reconciliation clears operation and increments version."""
    tx = Transaction(
        transaction_id="tx_res",
        status=TransactionStatus.PENDING_RECONCILIATION,
        version=4,
        pending_operation=op,
    )
    tx.resolve_reconciliation(target_resolution)

    assert tx.status == target_resolution
    assert tx.pending_operation is None
    assert tx.version == 5


@pytest.mark.parametrize(
    "stable_status,wrong_op",
    [
        (TransactionStatus.CREATED, PendingOperation.CAPTURE),
        (TransactionStatus.CREATED, PendingOperation.REFUND),
        (TransactionStatus.AUTHORIZED, PendingOperation.AUTHORIZATION),
        (TransactionStatus.AUTHORIZED, PendingOperation.REFUND),
        (TransactionStatus.CAPTURED, PendingOperation.AUTHORIZATION),
        (TransactionStatus.CAPTURED, PendingOperation.CAPTURE),
    ],
)
def test_invalid_reconciliation_operation_rejected(
    stable_status: TransactionStatus, wrong_op: PendingOperation
) -> None:
    """Verify starting reconciliation with mismatched operation is rejected without mutation."""
    tx = Transaction(transaction_id="tx_mismatch", status=stable_status, version=2)

    with pytest.raises(InvalidReconciliationOperationError) as exc_info:
        tx.mark_pending_reconciliation(wrong_op)

    assert exc_info.value.current_status == stable_status
    assert exc_info.value.attempted_operation == wrong_op
    assert exc_info.value.transaction_id == "tx_mismatch"

    # Invariant: Object remains unchanged on failure
    assert tx.status == stable_status
    assert tx.version == 2
    assert tx.pending_operation is None


@pytest.mark.parametrize(
    "op,illegal_resolution",
    [
        (PendingOperation.AUTHORIZATION, TransactionStatus.CAPTURED),
        (PendingOperation.AUTHORIZATION, TransactionStatus.REFUNDED),
        (PendingOperation.CAPTURE, TransactionStatus.DECLINED),
        (PendingOperation.CAPTURE, TransactionStatus.REFUNDED),
        (PendingOperation.CAPTURE, TransactionStatus.FAILED),
        (PendingOperation.REFUND, TransactionStatus.AUTHORIZED),
        (PendingOperation.REFUND, TransactionStatus.DECLINED),
        (PendingOperation.REFUND, TransactionStatus.FAILED),
    ],
)
def test_invalid_reconciliation_resolution_rejected(
    op: PendingOperation, illegal_resolution: TransactionStatus
) -> None:
    """Verify illegal reconciliation resolutions raise InvalidReconciliationResolutionError."""
    tx = Transaction(
        transaction_id="tx_bad_res",
        status=TransactionStatus.PENDING_RECONCILIATION,
        version=3,
        pending_operation=op,
    )

    with pytest.raises(InvalidReconciliationResolutionError) as exc_info:
        tx.resolve_reconciliation(illegal_resolution)

    assert exc_info.value.pending_operation == op
    assert exc_info.value.attempted_status == illegal_resolution
    assert exc_info.value.transaction_id == "tx_bad_res"

    # Invariant: Object remains unchanged on failure
    assert tx.status == TransactionStatus.PENDING_RECONCILIATION
    assert tx.version == 3
    assert tx.pending_operation == op


def test_mark_reconciliation_when_already_in_reconciliation_forbidden() -> None:
    """Verify cannot call mark_pending_reconciliation if already in PENDING_RECONCILIATION."""
    tx = Transaction(
        transaction_id="tx_double_rec",
        status=TransactionStatus.PENDING_RECONCILIATION,
        version=2,
        pending_operation=PendingOperation.AUTHORIZATION,
    )

    with pytest.raises(ReconciliationStateError):
        tx.mark_pending_reconciliation(PendingOperation.AUTHORIZATION)

    assert tx.version == 2


def test_resolve_reconciliation_when_not_in_reconciliation_forbidden() -> None:
    """Verify cannot call resolve_reconciliation if not in PENDING_RECONCILIATION."""
    tx = Transaction(transaction_id="tx_not_rec", status=TransactionStatus.CREATED, version=0)

    with pytest.raises(ReconciliationStateError):
        tx.resolve_reconciliation(TransactionStatus.AUTHORIZED)

    assert tx.status == TransactionStatus.CREATED
    assert tx.version == 0


@pytest.mark.parametrize(
    "terminal_status",
    [TransactionStatus.DECLINED, TransactionStatus.FAILED, TransactionStatus.REFUNDED],
)
def test_terminal_states_reject_reconciliation_entry(terminal_status: TransactionStatus) -> None:
    """Verify terminal states reject entering reconciliation."""
    tx = Transaction(transaction_id="tx_term_rec", status=terminal_status, version=5)
    assert tx.is_terminal() is True

    for op in PendingOperation:
        with pytest.raises(InvalidReconciliationOperationError):
            tx.mark_pending_reconciliation(op)

    assert tx.status == terminal_status
    assert tx.version == 5


# ---------------------------------------------------------------------------
# 5. Complete Lifecycles with Ambiguity
# ---------------------------------------------------------------------------


def test_complete_authorization_timeout_reconciliation_lifecycle() -> None:
    """Verify auth timeout workflow where inquiry confirms authorization succeeded."""
    tx = Transaction(transaction_id="tx_e2e_auth", version=0)
    assert tx.status == TransactionStatus.CREATED
    assert tx.version == 0

    # Downstream timeout -> mark pending reconciliation
    tx.mark_pending_reconciliation(PendingOperation.AUTHORIZATION)
    assert tx.status == TransactionStatus.PENDING_RECONCILIATION
    assert tx.pending_operation == PendingOperation.AUTHORIZATION
    assert tx.version == 1

    # Network confirmed authorization succeeded
    tx.resolve_reconciliation(TransactionStatus.AUTHORIZED)
    assert tx.status == TransactionStatus.AUTHORIZED
    assert tx.pending_operation is None
    assert tx.version == 2

    # Capture
    tx.transition_to(TransactionStatus.CAPTURED)
    assert tx.status == TransactionStatus.CAPTURED
    assert tx.version == 3

    # Refund
    tx.transition_to(TransactionStatus.REFUNDED)
    assert tx.status == TransactionStatus.REFUNDED
    assert tx.version == 4
    assert tx.is_terminal() is True


def test_complete_capture_timeout_revert_lifecycle() -> None:
    """Verify capture timeout workflow where capture did not occur and reverts to AUTHORIZED."""
    tx = Transaction(transaction_id="tx_e2e_cap")
    tx.transition_to(TransactionStatus.AUTHORIZED)
    assert tx.version == 1

    # Capture times out
    tx.mark_pending_reconciliation(PendingOperation.CAPTURE)
    assert tx.status == TransactionStatus.PENDING_RECONCILIATION
    assert tx.pending_operation == PendingOperation.CAPTURE
    assert tx.version == 2

    # Inquiry confirms capture did not occur; revert back to AUTHORIZED
    tx.resolve_reconciliation(TransactionStatus.AUTHORIZED)
    assert tx.status == TransactionStatus.AUTHORIZED
    assert tx.pending_operation is None
    assert tx.version == 3

    # Subsequent capture succeeds
    tx.transition_to(TransactionStatus.CAPTURED)
    assert tx.status == TransactionStatus.CAPTURED
    assert tx.version == 4


def test_complete_refund_timeout_lifecycle() -> None:
    """Verify refund timeout workflow where refund confirmed succeeded."""
    tx = Transaction(transaction_id="tx_e2e_ref")
    tx.transition_to(TransactionStatus.AUTHORIZED)
    tx.transition_to(TransactionStatus.CAPTURED)
    assert tx.version == 2

    # Refund times out
    tx.mark_pending_reconciliation(PendingOperation.REFUND)
    assert tx.status == TransactionStatus.PENDING_RECONCILIATION
    assert tx.version == 3

    # Inquiry confirms refund processed
    tx.resolve_reconciliation(TransactionStatus.REFUNDED)
    assert tx.status == TransactionStatus.REFUNDED
    assert tx.version == 4
    assert tx.is_terminal() is True
