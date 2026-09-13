"""Integration tests verifying PostgreSQL concurrent execution behavior with deterministic
coordination."""

import asyncio

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from transaction_platform.api.app import create_app as create_transaction_app
from transaction_platform.ports.payment_network import (
    PaymentNetworkAuthorizationRequest,
    PaymentNetworkAuthorizedResult,
    PaymentNetworkDeclinedResult,
    PaymentNetworkPort,
)


class CountingCoordinatedNetwork(PaymentNetworkPort):
    """Counting fake implementing PaymentNetworkPort for deterministic concurrency tests."""

    def __init__(self) -> None:
        self.call_count = 0
        self.call_started = asyncio.Event()
        self.can_complete = asyncio.Event()

    async def authorize(
        self, request: PaymentNetworkAuthorizationRequest
    ) -> PaymentNetworkAuthorizedResult | PaymentNetworkDeclinedResult:
        self.call_count += 1
        self.call_started.set()
        await self.can_complete.wait()
        return PaymentNetworkAuthorizedResult(
            network_reference=f"net_{request.transaction_id}",
            authorization_code="AUTH_TEST_123",
        )


class TestConcurrentIdempotentRequests:
    """Verify concurrent request isolation and conflict classification in PostgreSQL."""

    async def test_concurrent_identical_requests_calls_network_once_and_replays(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        fake_network = CountingCoordinatedNetwork()
        app = create_transaction_app(
            payment_network=fake_network,
            session_factory=session_factory,
        )
        key = "concurrent_key_100"
        payload = {
            "transaction_id": "txn_conc_100",
            "merchant_id": "merchant_101",
            "payment_token": "tok_test_approved",
            "amount": 2500,
            "currency": "USD",
        }

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Start the winning request
            winner_task = asyncio.create_task(
                client.post(
                    "/v1/transactions",
                    json=payload,
                    headers={"X-Correlation-ID": "corr_winner", "Idempotency-Key": key},
                )
            )

            # 2. Wait until the fake confirms the network call has begun
            await fake_network.call_started.wait()

            # 3. Start the competing requests while the winner is deliberately blocked
            async def send_competitor(i: int) -> tuple[int, dict[str, str], dict[str, object]]:
                res = await client.post(
                    "/v1/transactions",
                    json=payload,
                    headers={"X-Correlation-ID": f"corr_comp_{i}", "Idempotency-Key": key},
                )
                return res.status_code, dict(res.headers), res.json()

            comp_results = await asyncio.gather(*[send_competitor(i) for i in range(5)])

            # 4. Assert the competitors receive the expected 409 responses
            for code, headers, body in comp_results:
                assert code == 409
                assert body["type"] == "urn:problem-type:idempotency-in-progress"
                assert headers.get("retry-after") == "1"

            # 5. Assert call_count == 1
            assert fake_network.call_count == 1

            # 6. Release the fake
            fake_network.can_complete.set()

            # 7. Await the winner and assert HTTP 201
            winner_res = await winner_task
            assert winner_res.status_code == 201
            assert winner_res.json()["status"] == "AUTHORIZED"
            assert winner_res.json()["transaction_id"] == "txn_conc_100"

            # 8. Send a completed replay and assert HTTP 201 with Idempotent-Replay: true
            replay_res = await client.post(
                "/v1/transactions",
                json=payload,
                headers={"X-Correlation-ID": "corr_replay", "Idempotency-Key": key},
            )
            assert replay_res.status_code == 201
            assert replay_res.headers.get("Idempotent-Replay") == "true"
            assert replay_res.json()["status"] == "AUTHORIZED"

            # 9. Assert call_count remains exactly 1
            assert fake_network.call_count == 1

    async def test_concurrent_same_key_same_payload_race_coordination(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        fake_network = CountingCoordinatedNetwork()
        app = create_transaction_app(
            payment_network=fake_network,
            session_factory=session_factory,
        )
        key = "concurrent_key_200"
        payload = {
            "transaction_id": "txn_conc_200",
            "merchant_id": "merchant_101",
            "payment_token": "tok_test_approved",
            "amount": 1500,
            "currency": "USD",
        }

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Start the winning request
            winner_task = asyncio.create_task(
                client.post(
                    "/v1/transactions",
                    json=payload,
                    headers={"X-Correlation-ID": "corr_winner_200", "Idempotency-Key": key},
                )
            )

            # 2. Wait until the fake confirms the network call has begun
            await fake_network.call_started.wait()

            # 3. Start a competing identical request while the winner is blocked
            comp_res = await client.post(
                "/v1/transactions",
                json=payload,
                headers={"X-Correlation-ID": "corr_comp_200", "Idempotency-Key": key},
            )

            # 4. Assert competitor receives 409 idempotency-in-progress
            assert comp_res.status_code == 409
            assert comp_res.json()["type"] == "urn:problem-type:idempotency-in-progress"

            # 5. Assert call_count == 1
            assert fake_network.call_count == 1

            # 6. Release the fake
            fake_network.can_complete.set()

            # 7. Await winner and assert HTTP 201
            winner_res = await winner_task
            assert winner_res.status_code == 201
            assert winner_res.json()["status"] == "AUTHORIZED"

            # 8. Send replay and assert HTTP 201 with Idempotent-Replay
            replay_res = await client.post(
                "/v1/transactions",
                json=payload,
                headers={"X-Correlation-ID": "corr_replay_200", "Idempotency-Key": key},
            )
            assert replay_res.status_code == 201
            assert replay_res.headers.get("Idempotent-Replay") == "true"

            # 9. Assert call_count remains exactly 1
            assert fake_network.call_count == 1

    async def test_concurrent_different_keys_same_transaction_id_yields_transaction_id_conflict(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        fake_network = CountingCoordinatedNetwork()
        app = create_transaction_app(
            payment_network=fake_network,
            session_factory=session_factory,
        )
        payload = {
            "transaction_id": "txn_shared_conflict_300",
            "merchant_id": "merchant_101",
            "payment_token": "tok_test_approved",
            "amount": 4000,
            "currency": "USD",
        }

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Start the first request with key_alpha
            winner_task = asyncio.create_task(
                client.post(
                    "/v1/transactions",
                    json=payload,
                    headers={
                        "X-Correlation-ID": "corr_alpha",
                        "Idempotency-Key": "key_alpha_300",
                    },
                )
            )

            # 2. Wait until fake confirms network call has begun
            await fake_network.call_started.wait()

            # 3. Start competitor with key_beta (different key, same transaction_id)
            comp_res = await client.post(
                "/v1/transactions",
                json=payload,
                headers={
                    "X-Correlation-ID": "corr_beta",
                    "Idempotency-Key": "key_beta_300",
                },
            )

            # 4. Assert competitor receives 409 transaction-id-conflict
            assert comp_res.status_code == 409
            assert comp_res.json()["type"] == "urn:problem-type:transaction-id-conflict"

            # 5. Assert call_count == 1
            assert fake_network.call_count == 1

            # 6. Release the fake
            fake_network.can_complete.set()

            # 7. Await winner and assert HTTP 201
            winner_res = await winner_task
            assert winner_res.status_code == 201
            assert winner_res.json()["status"] == "AUTHORIZED"

            # 8. Send replay with key_alpha and assert HTTP 201 with Idempotent-Replay
            replay_res = await client.post(
                "/v1/transactions",
                json=payload,
                headers={
                    "X-Correlation-ID": "corr_replay_alpha",
                    "Idempotency-Key": "key_alpha_300",
                },
            )
            assert replay_res.status_code == 201
            assert replay_res.headers.get("Idempotent-Replay") == "true"

            # 9. Assert call_count remains exactly 1
            assert fake_network.call_count == 1
