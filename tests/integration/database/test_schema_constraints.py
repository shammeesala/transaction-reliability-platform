"""Integration tests verifying all PostgreSQL table check constraints."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class TestTransactionsCheckConstraints:
    """Verify PostgreSQL check constraints on the transactions table."""

    async def test_empty_or_whitespace_transaction_id_rejected(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        for invalid_id in ["", "   ", " " * 10]:
            async with session_factory() as session:
                with pytest.raises(IntegrityError) as exc_info:
                    async with session.begin():
                        await session.execute(
                            text(
                                "INSERT INTO transactions "
                                "(transaction_id, merchant_id, amount, currency, status) "
                                "VALUES (:tid, 'm1', 100, 'USD', 'CREATED')"
                            ),
                            {"tid": invalid_id},
                        )
                assert "chk_transactions_transaction_id_trimmed_length" in str(exc_info.value)

    async def test_empty_or_whitespace_merchant_id_rejected(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        for invalid_m in ["", "   ", " " * 10]:
            async with session_factory() as session:
                with pytest.raises(IntegrityError) as exc_info:
                    async with session.begin():
                        await session.execute(
                            text(
                                "INSERT INTO transactions "
                                "(transaction_id, merchant_id, amount, currency, status) "
                                "VALUES ('t1', :mid, 100, 'USD', 'CREATED')"
                            ),
                            {"mid": invalid_m},
                        )
                assert "chk_transactions_merchant_id_trimmed_length" in str(exc_info.value)

    async def test_non_positive_amount_rejected(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        for invalid_amt in [0, -100]:
            async with session_factory() as session:
                with pytest.raises(IntegrityError) as exc_info:
                    async with session.begin():
                        await session.execute(
                            text(
                                "INSERT INTO transactions "
                                "(transaction_id, merchant_id, amount, currency, status) "
                                "VALUES ('t1', 'm1', :amt, 'USD', 'CREATED')"
                            ),
                            {"amt": invalid_amt},
                        )
                assert "chk_transactions_amount_positive" in str(exc_info.value)

    async def test_invalid_currency_format_rejected(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        for invalid_curr in ["usd", "US", "123", "US1"]:
            async with session_factory() as session:
                with pytest.raises(IntegrityError) as exc_info:
                    async with session.begin():
                        await session.execute(
                            text(
                                "INSERT INTO transactions "
                                "(transaction_id, merchant_id, amount, currency, status) "
                                "VALUES ('t1', 'm1', 100, :curr, 'CREATED')"
                            ),
                            {"curr": invalid_curr},
                        )
                assert "chk_transactions_currency_format" in str(exc_info.value)

    async def test_negative_version_rejected(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            with pytest.raises(IntegrityError) as exc_info:
                async with session.begin():
                    await session.execute(
                        text(
                            "INSERT INTO transactions "
                            "(transaction_id, merchant_id, amount, currency, status, version) "
                            "VALUES ('t1', 'm1', 100, 'USD', 'CREATED', -1)"
                        )
                    )
            assert "chk_transactions_version_non_negative" in str(exc_info.value)

    async def test_invalid_status_rejected(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            with pytest.raises(IntegrityError) as exc_info:
                async with session.begin():
                    await session.execute(
                        text(
                            "INSERT INTO transactions "
                            "(transaction_id, merchant_id, amount, currency, status) "
                            "VALUES ('t1', 'm1', 100, 'USD', 'BOGUS_STATUS')"
                        )
                    )
            assert "chk_transactions_status_valid" in str(exc_info.value)

    async def test_invalid_pending_operation_rejected(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            with pytest.raises(IntegrityError) as exc_info:
                async with session.begin():
                    await session.execute(
                        text(
                            "INSERT INTO transactions "
                            "(transaction_id, merchant_id, amount, currency, status, "
                            "pending_operation) "
                            "VALUES ('t1', 'm1', 100, 'USD', 'PENDING_RECONCILIATION', 'BOGUS_OP')"
                        )
                    )
            assert "chk_transactions_pending_operation_valid" in str(exc_info.value)

    async def test_reconciliation_invariant_enforced(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            # Case A: PENDING_RECONCILIATION without pending_operation -> rejected
            with pytest.raises(IntegrityError) as exc_info:
                async with session.begin():
                    await session.execute(
                        text(
                            "INSERT INTO transactions "
                            "(transaction_id, merchant_id, amount, currency, status, "
                            "pending_operation) "
                            "VALUES ('t1', 'm1', 100, 'USD', 'PENDING_RECONCILIATION', NULL)"
                        )
                    )
            assert "chk_transactions_reconciliation_invariant" in str(exc_info.value)

            # Case B: CREATED with pending_operation -> rejected
            with pytest.raises(IntegrityError) as exc_info:
                async with session.begin():
                    await session.execute(
                        text(
                            "INSERT INTO transactions "
                            "(transaction_id, merchant_id, amount, currency, status, "
                            "pending_operation) "
                            "VALUES ('t2', 'm1', 100, 'USD', 'CREATED', 'AUTHORIZATION')"
                        )
                    )
            assert "chk_transactions_reconciliation_invariant" in str(exc_info.value)


class TestIdempotencyRecordsCheckConstraints:
    """Verify PostgreSQL check constraints on the idempotency_records table."""

    async def _insert_valid_txn(
        self, session_factory: async_sessionmaker[AsyncSession], txn_id: str
    ) -> None:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO transactions "
                        "(transaction_id, merchant_id, amount, currency, status) "
                        "VALUES (:tid, 'm1', 100, 'USD', 'CREATED')"
                    ),
                    {"tid": txn_id},
                )

    async def test_invalid_key_hash_format_rejected(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await self._insert_valid_txn(session_factory, "t1")
        valid_fp = "a" * 64
        for invalid_hash in ["a" * 63, "g" * 64, "A" * 64]:
            async with session_factory() as session:
                with pytest.raises(IntegrityError) as exc_info:
                    async with session.begin():
                        await session.execute(
                            text(
                                "INSERT INTO idempotency_records "
                                "(idempotency_key_hash, request_fingerprint, "
                                "processing_status, transaction_id) "
                                "VALUES (:h, :fp, 'IN_PROGRESS', 't1')"
                            ),
                            {"h": invalid_hash, "fp": valid_fp},
                        )
                assert "chk_idempotency_key_hash_format" in str(exc_info.value)

    async def test_invalid_fingerprint_format_rejected(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await self._insert_valid_txn(session_factory, "t1")
        valid_hash = "a" * 64
        for invalid_fp in ["b" * 63, "g" * 64, "B" * 64]:
            async with session_factory() as session:
                with pytest.raises(IntegrityError) as exc_info:
                    async with session.begin():
                        await session.execute(
                            text(
                                "INSERT INTO idempotency_records "
                                "(idempotency_key_hash, request_fingerprint, "
                                "processing_status, transaction_id) "
                                "VALUES (:h, :fp, 'IN_PROGRESS', 't1')"
                            ),
                            {"h": valid_hash, "fp": invalid_fp},
                        )
                assert "chk_idempotency_fingerprint_format" in str(exc_info.value)

    async def test_invalid_processing_status_rejected(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await self._insert_valid_txn(session_factory, "t1")
        async with session_factory() as session:
            with pytest.raises(IntegrityError) as exc_info:
                async with session.begin():
                    await session.execute(
                        text(
                            "INSERT INTO idempotency_records "
                            "(idempotency_key_hash, request_fingerprint, "
                            "processing_status, transaction_id) "
                            "VALUES (:h, :fp, 'INVALID_STATUS', 't1')"
                        ),
                        {"h": "a" * 64, "fp": "b" * 64},
                    )
            # Rejection can trigger chk_idempotency_status_valid or
            # chk_idempotency_payload_integrity
            err = str(exc_info.value)
            assert (
                "chk_idempotency_status_valid" in err
                or "chk_idempotency_payload_integrity" in err
            )

    async def test_payload_integrity_constraint_enforced(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await self._insert_valid_txn(session_factory, "t1")
        h = "a" * 64
        fp = "b" * 64
        async with session_factory() as session:
            # COMPLETED with NULL status code
            with pytest.raises(IntegrityError) as exc_info:
                async with session.begin():
                    await session.execute(
                        text(
                            "INSERT INTO idempotency_records "
                            "(idempotency_key_hash, request_fingerprint, processing_status, "
                            "transaction_id, stored_status_code, stored_response_body) "
                            "VALUES (:h, :fp, 'COMPLETED', 't1', NULL, '{\"status\": \"ok\"}')"
                        ),
                        {"h": h, "fp": fp},
                    )
            assert "chk_idempotency_payload_integrity" in str(exc_info.value)

        async with session_factory() as session:
            # IN_PROGRESS with non-NULL status code
            with pytest.raises(IntegrityError) as exc_info:
                async with session.begin():
                    await session.execute(
                        text(
                            "INSERT INTO idempotency_records "
                            "(idempotency_key_hash, request_fingerprint, processing_status, "
                            "transaction_id, stored_status_code) "
                            "VALUES (:h, :fp, 'IN_PROGRESS', 't1', 201)"
                        ),
                        {"h": h, "fp": fp},
                    )
            assert "chk_idempotency_payload_integrity" in str(exc_info.value)

        async with session_factory() as session:
            # IN_PROGRESS with non-NULL stored_headers
            with pytest.raises(IntegrityError) as exc_info:
                async with session.begin():
                    await session.execute(
                        text(
                            "INSERT INTO idempotency_records "
                            "(idempotency_key_hash, request_fingerprint, processing_status, "
                            "transaction_id, stored_headers) "
                            "VALUES (:h, :fp, 'IN_PROGRESS', 't1', '{\"Retry-After\": \"1\"}')"
                        ),
                        {"h": h, "fp": fp},
                    )
            assert "chk_idempotency_payload_integrity" in str(exc_info.value)

    async def test_status_code_range_enforced(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await self._insert_valid_txn(session_factory, "t1")
        h = "a" * 64
        fp = "b" * 64
        for invalid_code in [99, 600]:
            async with session_factory() as session:
                with pytest.raises(IntegrityError) as exc_info:
                    async with session.begin():
                        await session.execute(
                            text(
                                "INSERT INTO idempotency_records "
                                "(idempotency_key_hash, request_fingerprint, processing_status, "
                                "transaction_id, stored_status_code, stored_response_body) "
                                "VALUES (:h, :fp, 'COMPLETED', 't1', :code, '{\"status\": \"ok\"}')"
                            ),
                            {"h": h, "fp": fp, "code": invalid_code},
                        )
                assert "chk_idempotency_stored_status_code_range" in str(exc_info.value)

    async def test_stored_headers_allowlist_enforced(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await self._insert_valid_txn(session_factory, "t1")
        h = "a" * 64
        fp = "b" * 64
        async with session_factory() as session:
            # Arbitrary header (X-Correlation-ID) rejected
            with pytest.raises(IntegrityError) as exc_info:
                async with session.begin():
                    await session.execute(
                        text(
                            "INSERT INTO idempotency_records "
                            "(idempotency_key_hash, request_fingerprint, processing_status, "
                            "transaction_id, stored_status_code, stored_response_body, "
                            "stored_headers) "
                            "VALUES (:h, :fp, 'COMPLETED', 't1', 503, '{}', "
                            "'{\"X-Correlation-ID\": \"abc\"}')"
                        ),
                        {"h": h, "fp": fp},
                    )
            assert "chk_idempotency_stored_headers_allowlist" in str(exc_info.value)

        async with session_factory() as session:
            # Valid Retry-After header accepted
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO idempotency_records "
                        "(idempotency_key_hash, request_fingerprint, processing_status, "
                        "transaction_id, stored_status_code, stored_response_body, stored_headers) "
                        "VALUES (:h, :fp, 'COMPLETED', 't1', 503, '{}', '{\"Retry-After\": \"5\"}')"
                    ),
                    {"h": h, "fp": fp},
                )
