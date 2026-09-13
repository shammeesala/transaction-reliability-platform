"""Integration tests for SqlAlchemyIdempotencyCoordinator reservation and completion."""

from unittest.mock import patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from transaction_platform.common.idempotency import compute_key_hash, compute_request_fingerprint
from transaction_platform.domain.transaction import Transaction, TransactionStatus
from transaction_platform.infrastructure.idempotency_coordinator import (
    SqlAlchemyIdempotencyCoordinator,
)
from transaction_platform.infrastructure.models import idempotency_records_table, transactions_table
from transaction_platform.ports.idempotency_coordinator import (
    PersistenceError,
    ReservationStatus,
    ResponseEnvelope,
)


class TestCoordinatorReservationAndCompletion:
    """Verify atomic unit-of-work reservation, post-conflict classification, and completion."""

    async def test_atomic_reservation_success(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        coordinator = SqlAlchemyIdempotencyCoordinator(session_factory=session_factory)
        key_hash = compute_key_hash("key_100")
        fp = compute_request_fingerprint(
            amount=5000,
            currency="USD",
            merchant_id="m1",
            payment_token="tok_test_approved",
            transaction_id="t100",
        )

        res = await coordinator.reserve(
            key_hash=key_hash,
            request_fingerprint=fp,
            transaction_id="t100",
            merchant_id="m1",
            amount=5000,
            currency="USD",
        )
        assert res.status == ReservationStatus.ACQUIRED

        # Verify both rows exist with correct initial states
        async with session_factory() as session:
            txn_res = await session.execute(
                select(transactions_table).where(transactions_table.c.transaction_id == "t100")
            )
            txn_row = txn_res.mappings().one()
            assert txn_row["status"] == "CREATED"
            assert txn_row["version"] == 0

            idemp_res = await session.execute(
                select(idempotency_records_table).where(
                    idempotency_records_table.c.idempotency_key_hash == key_hash
                )
            )
            idemp_row = idemp_res.mappings().one()
            assert idemp_row["processing_status"] == "IN_PROGRESS"
            assert idemp_row["transaction_id"] == "t100"

    async def test_atomic_reservation_rollback_on_failure(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Verify that if idempotency insert fails, the transaction insert is rolled back."""
        coordinator = SqlAlchemyIdempotencyCoordinator(session_factory=session_factory)

        # Pre-insert idempotency record for key_200 referencing existing t_existing
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO transactions "
                        "(transaction_id, merchant_id, amount, currency, status) "
                        "VALUES ('t_existing', 'm1', 100, 'USD', 'CREATED')"
                    )
                )
                await session.execute(
                    text(
                        "INSERT INTO idempotency_records "
                        "(idempotency_key_hash, request_fingerprint, "
                        "processing_status, transaction_id) "
                        "VALUES (:h, :fp, 'IN_PROGRESS', 't_existing')"
                    ),
                    {"h": compute_key_hash("key_200"), "fp": "a" * 64},
                )

        # Now attempt reserve for new transaction 't_orphan_candidate' but reusing key_200
        # with different payload. It should observe existing key and return IDEMPOTENCY_CONFLICT,
        # and 't_orphan_candidate' is NOT created!
        fp_new = compute_request_fingerprint(
            amount=2000,
            currency="USD",
            merchant_id="m1",
            payment_token="tok_test_approved",
            transaction_id="t_orphan_candidate",
        )
        res = await coordinator.reserve(
            key_hash=compute_key_hash("key_200"),
            request_fingerprint=fp_new,
            transaction_id="t_orphan_candidate",
            merchant_id="m1",
            amount=2000,
            currency="USD",
        )
        assert res.status == ReservationStatus.IDEMPOTENCY_CONFLICT

        async with session_factory() as session:
            check_res = await session.execute(
                select(transactions_table).where(
                    transactions_table.c.transaction_id == "t_orphan_candidate"
                )
            )
            assert check_res.scalar_one_or_none() is None

    async def test_concurrent_identical_requests_never_mislabeled_as_transaction_id_conflict(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Test that concurrent identical requests are classified as IN_PROGRESS, not
        TRANSACTION_ID_CONFLICT."""
        coordinator = SqlAlchemyIdempotencyCoordinator(session_factory=session_factory)
        key_hash = compute_key_hash("concurrent_key_300")
        fp = compute_request_fingerprint(
            amount=3000,
            currency="USD",
            merchant_id="m1",
            payment_token="tok_test_approved",
            transaction_id="t300",
        )

        # First caller wins reservation
        res1 = await coordinator.reserve(
            key_hash=key_hash,
            request_fingerprint=fp,
            transaction_id="t300",
            merchant_id="m1",
            amount=3000,
            currency="USD",
        )
        assert res1.status == ReservationStatus.ACQUIRED

        # Second caller with the same key and same transaction ID calls reserve
        # Even though t300 exists in transactions, post-conflict re-query detects matching key_hash
        res2 = await coordinator.reserve(
            key_hash=key_hash,
            request_fingerprint=fp,
            transaction_id="t300",
            merchant_id="m1",
            amount=3000,
            currency="USD",
        )
        # MUST be IN_PROGRESS, NEVER TRANSACTION_ID_CONFLICT
        assert res2.status == ReservationStatus.IN_PROGRESS

    async def test_concurrent_insert_after_completion_returns_completed_replay(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        coordinator = SqlAlchemyIdempotencyCoordinator(session_factory=session_factory)
        key_hash = compute_key_hash("key_400")
        fp = compute_request_fingerprint(
            amount=4000,
            currency="USD",
            merchant_id="m1",
            payment_token="tok_test_approved",
            transaction_id="t400",
        )

        await coordinator.reserve(
            key_hash=key_hash,
            request_fingerprint=fp,
            transaction_id="t400",
            merchant_id="m1",
            amount=4000,
            currency="USD",
        )

        txn = Transaction(transaction_id="t400")
        txn.transition_to(TransactionStatus.AUTHORIZED)
        envelope = ResponseEnvelope(status_code=201, body={"status": "AUTHORIZED", "version": 1})

        await coordinator.complete(
            key_hash=key_hash,
            transaction=txn,
            expected_version=0,
            envelope=envelope,
        )

        # Replay with same key and payload
        res = await coordinator.reserve(
            key_hash=key_hash,
            request_fingerprint=fp,
            transaction_id="t400",
            merchant_id="m1",
            amount=4000,
            currency="USD",
        )
        assert res.status == ReservationStatus.COMPLETED
        assert res.stored_envelope is not None
        assert res.stored_envelope.status_code == 201
        assert res.stored_envelope.is_replay is True

    async def test_transaction_id_conflict_when_key_does_not_exist(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        coordinator = SqlAlchemyIdempotencyCoordinator(session_factory=session_factory)

        # First request reserves t500 under key_500
        fp1 = compute_request_fingerprint(
            amount=5000,
            currency="USD",
            merchant_id="m1",
            payment_token="tok_test_approved",
            transaction_id="t500",
        )
        await coordinator.reserve(
            key_hash=compute_key_hash("key_500"),
            request_fingerprint=fp1,
            transaction_id="t500",
            merchant_id="m1",
            amount=5000,
            currency="USD",
        )

        # Second request attempts to use t500 under a different key_different
        fp2 = compute_request_fingerprint(
            amount=5000,
            currency="USD",
            merchant_id="m1",
            payment_token="tok_test_approved",
            transaction_id="t500",
        )
        res = await coordinator.reserve(
            key_hash=compute_key_hash("key_different"),
            request_fingerprint=fp2,
            transaction_id="t500",
            merchant_id="m1",
            amount=5000,
            currency="USD",
        )
        assert res.status == ReservationStatus.TRANSACTION_ID_CONFLICT

    async def test_header_filtering_persists_only_allowlisted_retry_after(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        coordinator = SqlAlchemyIdempotencyCoordinator(session_factory=session_factory)
        key_hash = compute_key_hash("key_600")
        fp = compute_request_fingerprint(
            amount=6000,
            currency="USD",
            merchant_id="m1",
            payment_token="tok_test_approved",
            transaction_id="t600",
        )
        await coordinator.reserve(
            key_hash=key_hash,
            request_fingerprint=fp,
            transaction_id="t600",
            merchant_id="m1",
            amount=6000,
            currency="USD",
        )

        txn = Transaction(transaction_id="t600")
        txn.transition_to(TransactionStatus.FAILED)

        # Response envelope with multiple headers, only Retry-After should be persisted
        envelope = ResponseEnvelope(
            status_code=503,
            body={"type": "urn:problem-type:provider-rate-limited"},
            headers={
                "Retry-After": "10",
                "X-Correlation-ID": "forbidden-cid-123",
                "Server": "nginx",
                "Idempotent-Replay": "true",
            },
        )

        await coordinator.complete(
            key_hash=key_hash,
            transaction=txn,
            expected_version=0,
            envelope=envelope,
        )

        async with session_factory() as session:
            res = await session.execute(
                select(idempotency_records_table.c.stored_headers).where(
                    idempotency_records_table.c.idempotency_key_hash == key_hash
                )
            )
            stored_headers = res.scalar_one()
            assert stored_headers == {"Retry-After": "10"}
            assert "X-Correlation-ID" not in stored_headers
            assert "Server" not in stored_headers
            assert "Idempotent-Replay" not in stored_headers

    async def test_unexpected_sqlalchemy_failure_during_reserve_raises_persistence_error(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        coordinator = SqlAlchemyIdempotencyCoordinator(session_factory=session_factory)
        key_hash = compute_key_hash("key_err_reserve")
        fp = "a" * 64
        with patch.object(
            AsyncSession,
            "execute",
            side_effect=OperationalError("statement timeout", {}, Exception("driver fail")),
        ):
            with pytest.raises(PersistenceError) as exc_info:
                await coordinator.reserve(
                    key_hash=key_hash,
                    request_fingerprint=fp,
                    transaction_id="t_err_res",
                    merchant_id="m1",
                    amount=1000,
                    currency="USD",
                )
            err_msg = str(exc_info.value)
            assert "Database persistence operation failed during reservation." in err_msg
            assert isinstance(exc_info.value.__cause__, OperationalError)
            assert "driver fail" not in err_msg
            assert "statement timeout" not in err_msg
            assert key_hash not in err_msg
            assert "t_err_res" not in err_msg

    async def test_unexpected_sqlalchemy_failure_during_complete_raises_persistence_error(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        coordinator = SqlAlchemyIdempotencyCoordinator(session_factory=session_factory)
        key_hash = compute_key_hash("key_err_complete")
        fp = compute_request_fingerprint(
            amount=1000,
            currency="USD",
            merchant_id="m1",
            payment_token="tok_test_approved",
            transaction_id="t_err_complete",
        )
        res = await coordinator.reserve(
            key_hash=key_hash,
            request_fingerprint=fp,
            transaction_id="t_err_complete",
            merchant_id="m1",
            amount=1000,
            currency="USD",
        )
        assert res.status == ReservationStatus.ACQUIRED

        txn = Transaction(transaction_id="t_err_complete")
        txn.transition_to(TransactionStatus.AUTHORIZED)
        envelope = ResponseEnvelope(
            status_code=201,
            body={"status": "AUTHORIZED"},
            headers={},
        )

        with patch.object(
            AsyncSession,
            "execute",
            side_effect=OperationalError("connection terminated", {}, Exception("socket drop")),
        ):
            with pytest.raises(PersistenceError) as exc_info:
                await coordinator.complete(
                    key_hash=key_hash,
                    transaction=txn,
                    expected_version=0,
                    envelope=envelope,
                )
            err_msg = str(exc_info.value)
            assert "Database persistence operation failed during completion." in err_msg
            assert isinstance(exc_info.value.__cause__, OperationalError)
            assert "socket drop" not in err_msg
            assert "connection terminated" not in err_msg
            assert key_hash not in err_msg
            assert "t_err_complete" not in err_msg
