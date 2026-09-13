"""SQLAlchemy implementation of the IdempotencyCoordinatorPort."""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import CursorResult, RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from transaction_platform.domain.transaction import Transaction
from transaction_platform.infrastructure.models import (
    idempotency_records_table,
    transactions_table,
)
from transaction_platform.ports.idempotency_coordinator import (
    IdempotencyCoordinatorPort,
    IdempotencyReservation,
    OptimisticConcurrencyError,
    PersistenceError,
    ReservationStatus,
    ResponseEnvelope,
)


class _ReservationIntegrityError(Exception):
    """Internal carrier for caught IntegrityError across transaction boundary."""

    def __init__(self, orig_exc: IntegrityError) -> None:
        super().__init__(str(orig_exc))
        self.orig_exc = orig_exc


class SqlAlchemyIdempotencyCoordinator(IdempotencyCoordinatorPort):
    """Unit-of-work coordinator for atomic reservation and completion."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _classify_existing(
        record: RowMapping | Mapping[str, Any],
        request_fingerprint: str,
    ) -> IdempotencyReservation:
        if record["request_fingerprint"] != request_fingerprint:
            return IdempotencyReservation(status=ReservationStatus.IDEMPOTENCY_CONFLICT)

        if record["processing_status"] == "IN_PROGRESS":
            return IdempotencyReservation(status=ReservationStatus.IN_PROGRESS)

        if record["processing_status"] == "COMPLETED":
            envelope = ResponseEnvelope(
                status_code=record["stored_status_code"],
                body=record["stored_response_body"],
                headers=record["stored_headers"] or {},
                is_replay=True,
                media_type=(
                    "application/problem+json"
                    if record["stored_status_code"] >= 400
                    else "application/json"
                ),
            )
            return IdempotencyReservation(
                status=ReservationStatus.COMPLETED,
                stored_envelope=envelope,
            )

        raise PersistenceError(f"Unexpected processing_status: {record['processing_status']}")

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
        # 1. Initial check & insert in Short Tx 1
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    stmt = select(idempotency_records_table).where(
                        idempotency_records_table.c.idempotency_key_hash == key_hash
                    )
                    res = await session.execute(stmt)
                    existing = res.mappings().first()
                    if existing is not None:
                        return self._classify_existing(existing, request_fingerprint)

                    try:
                        # Step A: Insert transaction
                        await session.execute(
                            insert(transactions_table).values(
                                transaction_id=transaction_id,
                                merchant_id=merchant_id,
                                amount=amount,
                                currency=currency,
                                status="CREATED",
                                pending_operation=None,
                                version=0,
                                created_at=func.now(),
                                updated_at=func.now(),
                            )
                        )
                        # Step B: Insert idempotency record
                        await session.execute(
                            insert(idempotency_records_table).values(
                                idempotency_key_hash=key_hash,
                                request_fingerprint=request_fingerprint,
                                processing_status="IN_PROGRESS",
                                transaction_id=transaction_id,
                                created_at=func.now(),
                                updated_at=func.now(),
                            )
                        )
                    except IntegrityError as exc:
                        # Automatic rollback performed by session.begin()
                        raise _ReservationIntegrityError(exc) from exc

            return IdempotencyReservation(status=ReservationStatus.ACQUIRED)

        except _ReservationIntegrityError as res_err:
            orig_exc = res_err.orig_exc

            try:
                # 2. Post-conflict re-query in a fresh session/transaction
                async with self._session_factory() as fresh_session:
                    async with fresh_session.begin():
                        stmt = select(idempotency_records_table).where(
                            idempotency_records_table.c.idempotency_key_hash == key_hash
                        )
                        res = await fresh_session.execute(stmt)
                        existing_after = res.mappings().first()
                        if existing_after is not None:
                            return self._classify_existing(existing_after, request_fingerprint)

                        # Key does not exist; check if transaction_id belongs to another key
                        stmt_txn = select(transactions_table.c.transaction_id).where(
                            transactions_table.c.transaction_id == transaction_id
                        )
                        res_txn = await fresh_session.execute(stmt_txn)
                        if res_txn.scalar_one_or_none() is not None:
                            return IdempotencyReservation(
                                status=ReservationStatus.TRANSACTION_ID_CONFLICT
                            )

                # Neither the key nor transaction ID explains the constraint failure
                raise PersistenceError(
                    "Reservation failed due to unexpected integrity constraint violation."
                ) from orig_exc
            except (OptimisticConcurrencyError, PersistenceError):
                raise
            except SQLAlchemyError as exc:
                raise PersistenceError(
                    "Database persistence operation failed during reservation."
                ) from exc
        except (OptimisticConcurrencyError, PersistenceError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(
                "Database persistence operation failed during reservation."
            ) from exc

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
        """Atomically update transaction and mark idempotency record COMPLETED."""
        if transaction.version != expected_version + 1:
            raise OptimisticConcurrencyError(
                f"Transaction {transaction.transaction_id} version {transaction.version} "
                f"does not match expected next version {expected_version + 1}."
            )

        # Enforce header allowlist before persistence: only Retry-After is allowed
        allowlisted_headers: dict[str, str] = {}
        for k, v in envelope.headers.items():
            if k.lower() == "retry-after":
                allowlisted_headers["Retry-After"] = str(v)

        persisted_headers: dict[str, str] | None = (
            allowlisted_headers if allowlisted_headers else None
        )

        try:
            async with self._session_factory() as session:
                async with session.begin():
                    # 1. Update transaction verifying expected_version
                    stmt_txn = (
                        update(transactions_table)
                        .where(
                            transactions_table.c.transaction_id == transaction.transaction_id,
                            transactions_table.c.version == expected_version,
                        )
                        .values(
                            status=transaction.status.value,
                            pending_operation=(
                                transaction.pending_operation.value
                                if transaction.pending_operation
                                else None
                            ),
                            version=transaction.version,
                            network_reference=network_reference,
                            authorization_code=authorization_code,
                            decline_code=decline_code,
                            updated_at=func.now(),
                        )
                    )
                    res_txn = await session.execute(stmt_txn)
                    if not isinstance(res_txn, CursorResult) or res_txn.rowcount != 1:
                        actual_count = res_txn.rowcount if isinstance(res_txn, CursorResult) else 0
                        raise OptimisticConcurrencyError(
                            f"Transaction update affected {actual_count} rows; "
                            f"expected exactly 1 for transaction_id={transaction.transaction_id}, "
                            f"version={expected_version}."
                        )

                    # 2. Update idempotency record verifying transaction_id and IN_PROGRESS status
                    stmt_idemp = (
                        update(idempotency_records_table)
                        .where(
                            idempotency_records_table.c.idempotency_key_hash == key_hash,
                            idempotency_records_table.c.transaction_id
                            == transaction.transaction_id,
                            idempotency_records_table.c.processing_status == "IN_PROGRESS",
                        )
                        .values(
                            processing_status="COMPLETED",
                            stored_status_code=envelope.status_code,
                            stored_response_body=envelope.body,
                            stored_headers=persisted_headers,
                            updated_at=func.now(),
                        )
                    )
                    res_idemp = await session.execute(stmt_idemp)
                    if not isinstance(res_idemp, CursorResult) or res_idemp.rowcount != 1:
                        actual_count = (
                            res_idemp.rowcount if isinstance(res_idemp, CursorResult) else 0
                        )
                        raise OptimisticConcurrencyError(
                            f"Idempotency record update affected {actual_count} rows; "
                            f"expected exactly 1 for key_hash={key_hash}, "
                            f"transaction_id={transaction.transaction_id}, status=IN_PROGRESS."
                        )
        except (OptimisticConcurrencyError, PersistenceError):
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(
                "Database persistence operation failed during completion."
            ) from exc
