"""Integration tests for optimistic locking and completion validation across both tables."""

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from transaction_platform.common.idempotency import compute_key_hash, compute_request_fingerprint
from transaction_platform.domain.transaction import Transaction, TransactionStatus
from transaction_platform.infrastructure.idempotency_coordinator import (
    SqlAlchemyIdempotencyCoordinator,
)
from transaction_platform.infrastructure.models import idempotency_records_table, transactions_table
from transaction_platform.ports.idempotency_coordinator import (
    OptimisticConcurrencyError,
    ReservationStatus,
    ResponseEnvelope,
)


class TestOptimisticLockingAndCompletionValidation:
    """Verify optimistic concurrency and unit-of-work completion invariants in PostgreSQL."""

    async def _setup_reservation(
        self,
        coordinator: SqlAlchemyIdempotencyCoordinator,
        txn_id: str,
        raw_key: str,
    ) -> tuple[str, str, Transaction]:
        key_hash = compute_key_hash(raw_key)
        fp = compute_request_fingerprint(
            amount=1000,
            currency="USD",
            merchant_id="m1",
            payment_token="tok_test_approved",
            transaction_id=txn_id,
        )
        res = await coordinator.reserve(
            key_hash=key_hash,
            request_fingerprint=fp,
            transaction_id=txn_id,
            merchant_id="m1",
            amount=1000,
            currency="USD",
        )
        assert res.status == ReservationStatus.ACQUIRED
        domain_txn = Transaction(transaction_id=txn_id)
        domain_txn.transition_to(TransactionStatus.AUTHORIZED)
        assert domain_txn.version == 1
        return key_hash, fp, domain_txn

    async def test_stale_transaction_version_rolls_back_completion(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        coordinator = SqlAlchemyIdempotencyCoordinator(session_factory=session_factory)
        key_hash, _, domain_txn = await self._setup_reservation(coordinator, "t1", "k1")

        # Simulate concurrent update that advanced transactions.version to 1
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("UPDATE transactions SET version = 1 WHERE transaction_id = 't1'")
                )

        envelope = ResponseEnvelope(
            status_code=201,
            body={"status": "AUTHORIZED"},
        )

        # Attempting to complete with expected_version=0 should fail due to version mismatch
        with pytest.raises(OptimisticConcurrencyError):
            await coordinator.complete(
                key_hash=key_hash,
                transaction=domain_txn,
                expected_version=0,
                envelope=envelope,
            )

        # Verify atomic rollback: idempotency record remains IN_PROGRESS and status is CREATED
        async with session_factory() as session:
            res = await session.execute(
                select(idempotency_records_table.c.processing_status).where(
                    idempotency_records_table.c.idempotency_key_hash == key_hash
                )
            )
            assert res.scalar_one() == "IN_PROGRESS"

            res_txn = await session.execute(
                select(transactions_table.c.status).where(
                    transactions_table.c.transaction_id == "t1"
                )
            )
            assert res_txn.scalar_one() == "CREATED"

    async def test_missing_idempotency_record_rolls_back_transaction_update(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        coordinator = SqlAlchemyIdempotencyCoordinator(session_factory=session_factory)
        key_hash, _, domain_txn = await self._setup_reservation(coordinator, "t2", "k2")

        # Manually delete idempotency record
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "DELETE FROM idempotency_records WHERE idempotency_key_hash = :h",
                    ),
                    {"h": key_hash},
                )

        envelope = ResponseEnvelope(status_code=201, body={"status": "AUTHORIZED"})

        with pytest.raises(OptimisticConcurrencyError):
            await coordinator.complete(
                key_hash=key_hash,
                transaction=domain_txn,
                expected_version=0,
                envelope=envelope,
            )

        # Verify atomic rollback: transaction is still CREATED and version 0
        async with session_factory() as session:
            res_txn = await session.execute(
                select(transactions_table.c.status, transactions_table.c.version).where(
                    transactions_table.c.transaction_id == "t2"
                )
            )
            row = res_txn.one()
            assert row.status == "CREATED"
            assert row.version == 0

    async def test_already_completed_idempotency_record_cannot_be_completed_again(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        coordinator = SqlAlchemyIdempotencyCoordinator(session_factory=session_factory)
        key_hash, _, domain_txn = await self._setup_reservation(coordinator, "t3", "k3")
        envelope = ResponseEnvelope(status_code=201, body={"status": "AUTHORIZED"})

        # First completion succeeds
        await coordinator.complete(
            key_hash=key_hash,
            transaction=domain_txn,
            expected_version=0,
            envelope=envelope,
        )

        # Second completion attempt must raise OptimisticConcurrencyError
        with pytest.raises(OptimisticConcurrencyError):
            await coordinator.complete(
                key_hash=key_hash,
                transaction=domain_txn,
                expected_version=0,
                envelope=envelope,
            )

    async def test_key_belonging_to_another_transaction_cannot_complete(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        coordinator = SqlAlchemyIdempotencyCoordinator(session_factory=session_factory)
        await self._setup_reservation(coordinator, "t4", "k4")
        key_hash_5, _, _ = await self._setup_reservation(coordinator, "t5", "k5")

        # Try to complete t4 using k5's key_hash
        domain_txn_4 = Transaction(transaction_id="t4")
        domain_txn_4.transition_to(TransactionStatus.AUTHORIZED)
        envelope = ResponseEnvelope(status_code=201, body={"status": "AUTHORIZED"})

        with pytest.raises(OptimisticConcurrencyError):
            await coordinator.complete(
                key_hash=key_hash_5,
                transaction=domain_txn_4,
                expected_version=0,
                envelope=envelope,
            )

        # Verify t4 remains CREATED and v=0
        async with session_factory() as session:
            res = await session.execute(
                select(transactions_table.c.status, transactions_table.c.version).where(
                    transactions_table.c.transaction_id == "t4"
                )
            )
            row = res.one()
            assert row.status == "CREATED"
            assert row.version == 0
