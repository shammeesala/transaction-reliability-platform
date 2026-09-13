"""Integration tests for PostgreSQL-backed HTTP idempotency, replays, and conflict handling."""

from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from transaction_platform.api.app import create_app as create_transaction_app
from transaction_platform.common.problem_details import PROBLEM_MEDIA_TYPE
from transaction_platform.emulator.app import create_app as create_emulator_app
from transaction_platform.emulator.settings import EmulatorSettings
from transaction_platform.infrastructure.models import idempotency_records_table, transactions_table
from transaction_platform.ports.idempotency_coordinator import (
    IdempotencyCoordinatorPort,
    IdempotencyReservation,
    PersistenceError,
)


@pytest.fixture
def emulator_app() -> FastAPI:
    settings = EmulatorSettings(timeout_delay_seconds=0.01)
    return create_emulator_app(settings=settings)


@pytest.fixture
def transaction_app(
    emulator_app: FastAPI, session_factory: async_sessionmaker[AsyncSession]
) -> FastAPI:
    emulator_client = AsyncClient(
        transport=ASGITransport(app=emulator_app),
        base_url="http://emulator.test",
    )
    return create_transaction_app(
        http_client=emulator_client,
        emulator_base_url="http://emulator.test",
        session_factory=session_factory,
    )


class MockFailingCoordinator(IdempotencyCoordinatorPort):
    """Coordinator fake simulating unexpected persistence database failures."""

    async def reserve(self, *args: Any, **kwargs: Any) -> IdempotencyReservation:
        raise PersistenceError("psycopg.OperationalError: server closed connection unexpectedly")

    async def complete(self, *args: Any, **kwargs: Any) -> None:
        raise PersistenceError("psycopg.OperationalError: server closed connection unexpectedly")


class TestIdempotencyApiLifecycle:
    """Verify HTTP idempotency lifecycle, header reflection, and durable persistence."""

    VALID_PAYLOAD = {
        "transaction_id": "txn_api_100",
        "merchant_id": "merchant_101",
        "payment_token": "tok_test_approved",
        "amount": 2500,
        "currency": "USD",
    }

    async def test_first_authorization_succeeds_and_persists_in_postgres(
        self, transaction_app: FastAPI, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        headers = {
            "X-Correlation-ID": "corr_init_100",
            "Idempotency-Key": "key_init_100",
        }
        transport = ASGITransport(app=transaction_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/transactions", json=self.VALID_PAYLOAD, headers=headers
            )
            assert response.status_code == 201
            data = response.json()
            assert data["transaction_id"] == "txn_api_100"
            assert data["status"] == "AUTHORIZED"
            assert data["version"] == 1
            assert "Idempotent-Replay" not in response.headers
            assert response.headers["X-Correlation-ID"] == "corr_init_100"

        # Verify DB records
        async with session_factory() as session:
            txn_res = await session.execute(
                select(transactions_table).where(
                    transactions_table.c.transaction_id == "txn_api_100"
                )
            )
            txn = txn_res.mappings().one()
            assert txn["status"] == "AUTHORIZED"
            assert txn["version"] == 1

            idemp_res = await session.execute(
                select(idempotency_records_table).where(
                    idempotency_records_table.c.transaction_id == "txn_api_100"
                )
            )
            idemp = idemp_res.mappings().one()
            assert idemp["processing_status"] == "COMPLETED"
            assert idemp["stored_status_code"] == 201

            # Verify token and raw idempotency key are nowhere in DB
            raw_token_check = await session.execute(
                text("SELECT count(*) FROM transactions WHERE merchant_id LIKE '%tok_test%'")
            )
            assert raw_token_check.scalar_one() == 0

    async def test_replay_reflects_new_correlation_id_and_not_original(
        self, transaction_app: FastAPI
    ) -> None:
        key = "key_replay_test_200"
        original_cid = "corr_original_200"
        replay_cid = "corr_replay_200"

        payload = {
            "transaction_id": "txn_api_200",
            "merchant_id": "merchant_101",
            "payment_token": "tok_test_approved",
            "amount": 3500,
            "currency": "USD",
        }

        transport = ASGITransport(app=transaction_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Initial Request
            res1 = await client.post(
                "/v1/transactions",
                json=payload,
                headers={"X-Correlation-ID": original_cid, "Idempotency-Key": key},
            )
            assert res1.status_code == 201
            assert res1.headers["X-Correlation-ID"] == original_cid
            assert "Idempotent-Replay" not in res1.headers

            # 2. Replayed Request with NEW correlation ID
            res2 = await client.post(
                "/v1/transactions",
                json=payload,
                headers={"X-Correlation-ID": replay_cid, "Idempotency-Key": key},
            )
            assert res2.status_code == 201
            assert res2.headers["X-Correlation-ID"] == replay_cid
            assert res2.headers["X-Correlation-ID"] != original_cid
            assert res2.headers["Idempotent-Replay"] == "true"
            assert res2.json() == res1.json()

    async def test_replay_with_payload_mismatch_returns_409_idempotency_conflict(
        self, transaction_app: FastAPI
    ) -> None:
        key = "key_conflict_300"
        payload1 = {
            "transaction_id": "txn_api_300",
            "merchant_id": "merchant_101",
            "payment_token": "tok_test_approved",
            "amount": 1000,
            "currency": "USD",
        }
        transport = ASGITransport(app=transaction_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res1 = await client.post(
                "/v1/transactions",
                json=payload1,
                headers={"X-Correlation-ID": "corr_301", "Idempotency-Key": key},
            )
            assert res1.status_code == 201

            # Reusing same key with changed amount
            payload2 = {**payload1, "amount": 2000}
            res2 = await client.post(
                "/v1/transactions",
                json=payload2,
                headers={"X-Correlation-ID": "corr_302", "Idempotency-Key": key},
            )
            assert res2.status_code == 409
            assert res2.headers["Content-Type"] == PROBLEM_MEDIA_TYPE
            data = res2.json()
            assert data["type"] == "urn:problem-type:idempotency-conflict"

    async def test_different_key_reusing_same_transaction_id_returns_409_conflict(
        self, transaction_app: FastAPI
    ) -> None:
        payload = {
            "transaction_id": "txn_shared_400",
            "merchant_id": "merchant_101",
            "payment_token": "tok_test_approved",
            "amount": 1000,
            "currency": "USD",
        }
        transport = ASGITransport(app=transaction_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res1 = await client.post(
                "/v1/transactions",
                json=payload,
                headers={"X-Correlation-ID": "corr_401", "Idempotency-Key": "key_first_400"},
            )
            assert res1.status_code == 201

            # Different key, same transaction_id
            res2 = await client.post(
                "/v1/transactions",
                json=payload,
                headers={"X-Correlation-ID": "corr_402", "Idempotency-Key": "key_second_400"},
            )
            assert res2.status_code == 409
            assert res2.headers["Content-Type"] == PROBLEM_MEDIA_TYPE
            data = res2.json()
            assert data["type"] == "urn:problem-type:transaction-id-conflict"

    async def test_missing_or_invalid_idempotency_key_returns_400(
        self, transaction_app: FastAPI
    ) -> None:
        transport = ASGITransport(app=transaction_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Missing
            res = await client.post(
                "/v1/transactions",
                json=self.VALID_PAYLOAD,
                headers={"X-Correlation-ID": "corr_500"},
            )
            assert res.status_code == 400
            assert res.json()["type"] == "urn:problem-type:missing-idempotency-key"

            # Invalid (contains whitespace)
            res_ws = await client.post(
                "/v1/transactions",
                json=self.VALID_PAYLOAD,
                headers={"X-Correlation-ID": "corr_500", "Idempotency-Key": "key with space"},
            )
            assert res_ws.status_code == 400
            assert res_ws.json()["type"] == "urn:problem-type:missing-idempotency-key"

    async def test_persistence_failure_returns_sanitized_500_with_correlation_id(
        self,
    ) -> None:
        # App configured with failing coordinator
        failing_app = create_transaction_app(
            coordinator=MockFailingCoordinator(),
        )
        transport = ASGITransport(app=failing_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cid = "corr_fail_600"
            res = await client.post(
                "/v1/transactions",
                json=self.VALID_PAYLOAD,
                headers={"X-Correlation-ID": cid, "Idempotency-Key": "key_600"},
            )
            assert res.status_code == 500
            assert res.headers["Content-Type"] == PROBLEM_MEDIA_TYPE
            assert res.headers["X-Correlation-ID"] == cid
            data = res.json()
            assert data["type"] == "urn:problem-type:internal-persistence-error"
            # Verify no database internals leaked
            assert "psycopg" not in str(data)
            assert "OperationalError" not in str(data)
            assert "closed connection" not in str(data)

    async def test_unexpected_sqlalchemy_failure_during_reservation_returns_sanitized_500(
        self, transaction_app: FastAPI
    ) -> None:
        transport = ASGITransport(app=transaction_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cid = "corr_res_err_700"
            with patch.object(
                AsyncSession,
                "execute",
                side_effect=OperationalError(
                    "db connection reset", {}, Exception("raw driver socket drop")
                ),
            ):
                res = await client.post(
                    "/v1/transactions",
                    json=self.VALID_PAYLOAD,
                    headers={"X-Correlation-ID": cid, "Idempotency-Key": "key_res_err_700"},
                )
                assert res.status_code == 500
                assert res.headers["Content-Type"] == PROBLEM_MEDIA_TYPE
                assert res.headers["X-Correlation-ID"] == cid
                data = res.json()
                assert data["type"] == "urn:problem-type:internal-persistence-error"
                assert "raw driver socket drop" not in res.text
                assert "db connection reset" not in res.text
                assert "OperationalError" not in res.text
                assert "transactions" not in res.text
                assert "idempotency_records" not in res.text
                assert self.VALID_PAYLOAD["payment_token"] not in res.text

    async def test_unexpected_sqlalchemy_failure_during_completion_returns_sanitized_500(
        self, transaction_app: FastAPI
    ) -> None:
        transport = ASGITransport(app=transaction_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cid = "corr_comp_err_800"
            orig_execute = AsyncSession.execute
            call_idx = 0

            async def execute_patch(
                self_session: AsyncSession, *args: Any, **kwargs: Any
            ) -> Any:
                nonlocal call_idx
                call_idx += 1
                # Reservation executes 2 statements. Completion executes statements after call 2.
                if call_idx > 2:
                    raise OperationalError(
                        "connection lost on update", {}, Exception("fatal socket termination")
                    )
                return await orig_execute(self_session, *args, **kwargs)

            with patch.object(AsyncSession, "execute", new=execute_patch):
                res = await client.post(
                    "/v1/transactions",
                    json=self.VALID_PAYLOAD,
                    headers={"X-Correlation-ID": cid, "Idempotency-Key": "key_comp_err_800"},
                )
                assert res.status_code == 500
                assert res.headers["Content-Type"] == PROBLEM_MEDIA_TYPE
                assert res.headers["X-Correlation-ID"] == cid
                data = res.json()
                assert data["type"] == "urn:problem-type:internal-persistence-error"
                assert "fatal socket termination" not in res.text
                assert "connection lost on update" not in res.text
                assert "OperationalError" not in res.text
                assert "transactions" not in res.text
                assert "idempotency_records" not in res.text
                assert self.VALID_PAYLOAD["payment_token"] not in res.text
