"""Integration tests for the main transaction API service."""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, MockTransport, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from transaction_platform.api.app import create_app as create_transaction_app
from transaction_platform.common.problem_details import PROBLEM_MEDIA_TYPE
from transaction_platform.emulator.app import create_app as create_emulator_app
from transaction_platform.emulator.settings import EmulatorSettings
from transaction_platform.ports.payment_network import (
    PaymentNetworkAuthorizationRequest,
    PaymentNetworkAuthorizedResult,
    PaymentNetworkDeclinedResult,
    PaymentNetworkPort,
    PaymentNetworkRejectedError,
    PaymentNetworkUnavailableError,
)


class ApiClientAdapter:
    """Synchronous test adapter wrapping AsyncClient and ASGITransport."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    def _request(self, method: str, url: str, **kwargs: Any) -> Response:
        async def _call() -> Response:
            transport = ASGITransport(app=self._app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                return await ac.request(method, url, **kwargs)

        return asyncio.run(_call())

    def get(self, url: str, **kwargs: Any) -> Response:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        if url == "/v1/transactions":
            headers = dict(kwargs.get("headers") or {})
            if "Idempotency-Key" not in headers:
                json_data = kwargs.get("json")
                if isinstance(json_data, dict) and "transaction_id" in json_data:
                    headers["Idempotency-Key"] = f"idem_{json_data['transaction_id']}"
                else:
                    headers["Idempotency-Key"] = "idem_default_test_key"
                kwargs["headers"] = headers
        return self._request("POST", url, **kwargs)


class MockFailingPort(PaymentNetworkPort):
    """Port fake for simulating upstream network rejections or transport crashes."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def authorize(
        self, request: PaymentNetworkAuthorizationRequest
    ) -> PaymentNetworkAuthorizedResult | PaymentNetworkDeclinedResult:
        raise self.error


@pytest.fixture
def emulator_app() -> FastAPI:
    settings = EmulatorSettings(timeout_delay_seconds=0.01)
    return create_emulator_app(settings=settings)


@pytest.fixture
def transaction_app(
    emulator_app: FastAPI, session_factory: async_sessionmaker[AsyncSession]
) -> Iterator[FastAPI]:
    emulator_client = AsyncClient(
        transport=ASGITransport(app=emulator_app),
        base_url="http://emulator.test",
    )
    app = create_transaction_app(
        http_client=emulator_client,
        emulator_base_url="http://emulator.test",
        session_factory=session_factory,
    )
    yield app
    asyncio.run(emulator_client.aclose())


@pytest.fixture
def client(transaction_app: FastAPI) -> ApiClientAdapter:
    return ApiClientAdapter(transaction_app)


class TestHealthEndpoint:
    """Verify service health and liveness."""

    def test_liveness_check(self, client: ApiClientAdapter) -> None:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestCorrelationIdEnforcement:
    """Verify correlation ID validation, rejection, and reflection invariants."""

    VALID_BODY = {
        "transaction_id": "txn_001",
        "merchant_id": "merchant_101",
        "payment_token": "tok_test_approved",
        "amount": 1000,
        "currency": "USD",
    }

    def test_missing_correlation_id_returns_http_400(self, client: ApiClientAdapter) -> None:
        response = client.post("/v1/transactions", json=self.VALID_BODY)
        assert response.status_code == 400
        assert response.headers["Content-Type"] == PROBLEM_MEDIA_TYPE
        data = response.json()
        assert data["type"] == "urn:problem-type:missing-correlation-id"
        assert "X-Correlation-ID" not in response.headers

    def test_blank_correlation_id_returns_http_400(self, client: ApiClientAdapter) -> None:
        response = client.post(
            "/v1/transactions",
            json=self.VALID_BODY,
            headers={"X-Correlation-ID": "   "},
        )
        assert response.status_code == 400
        assert "X-Correlation-ID" not in response.headers

    def test_whitespace_in_correlation_id_returns_http_400(
        self, client: ApiClientAdapter
    ) -> None:
        response = client.post(
            "/v1/transactions",
            json=self.VALID_BODY,
            headers={"X-Correlation-ID": "corr 123"},
        )
        assert response.status_code == 400
        assert "X-Correlation-ID" not in response.headers

    def test_invalid_characters_in_correlation_id_returns_http_400(
        self, client: ApiClientAdapter
    ) -> None:
        response = client.post(
            "/v1/transactions",
            json=self.VALID_BODY,
            headers={"X-Correlation-ID": "corr$invalid!"},
        )
        assert response.status_code == 400
        assert "X-Correlation-ID" not in response.headers

    def test_valid_correlation_id_reflected_on_success(self, client: ApiClientAdapter) -> None:
        cid = "corr_valid-123.test:alpha"
        response = client.post(
            "/v1/transactions",
            json=self.VALID_BODY,
            headers={"X-Correlation-ID": cid},
        )
        assert response.status_code == 201
        assert response.headers["X-Correlation-ID"] == cid


class TestRequestSchemaValidation:
    """Verify strict request boundary constraints and error sanitization."""

    HEADERS = {"X-Correlation-ID": "corr_valid_test"}

    def test_non_synthetic_payment_token_rejected(self, client: ApiClientAdapter) -> None:
        payload = {
            "transaction_id": "txn_002",
            "merchant_id": "merchant_101",
            "payment_token": "tok_live_real_card_123",
            "amount": 1000,
            "currency": "USD",
        }
        response = client.post("/v1/transactions", json=payload, headers=self.HEADERS)
        assert response.status_code == 422
        assert response.headers["Content-Type"] == PROBLEM_MEDIA_TYPE
        data = response.json()
        assert data["type"] == "urn:problem-type:validation-error"
        params = {p["field"]: p["reason"] for p in data["invalid_parameters"]}
        assert "payment_token" in params

    def test_boolean_amount_rejected(self, client: ApiClientAdapter) -> None:
        payload = {
            "transaction_id": "txn_003",
            "merchant_id": "merchant_101",
            "payment_token": "tok_test_approved",
            "amount": True,
            "currency": "USD",
        }
        response = client.post("/v1/transactions", json=payload, headers=self.HEADERS)
        assert response.status_code == 422
        data = response.json()
        params = {p["field"]: p["reason"] for p in data["invalid_parameters"]}
        assert "amount" in params

    def test_non_positive_amount_rejected(self, client: ApiClientAdapter) -> None:
        for invalid_amount in [0, -500]:
            payload = {
                "transaction_id": "txn_004",
                "merchant_id": "merchant_101",
                "payment_token": "tok_test_approved",
                "amount": invalid_amount,
                "currency": "USD",
            }
            response = client.post("/v1/transactions", json=payload, headers=self.HEADERS)
            assert response.status_code == 422
            data = response.json()
            params = {p["field"]: p["reason"] for p in data["invalid_parameters"]}
            assert "amount" in params

    def test_invalid_currency_format_rejected(self, client: ApiClientAdapter) -> None:
        for invalid_currency in ["US", "USDD", "usd", "123", "US!"]:
            payload = {
                "transaction_id": "txn_005",
                "merchant_id": "merchant_101",
                "payment_token": "tok_test_approved",
                "amount": 1000,
                "currency": invalid_currency,
            }
            response = client.post("/v1/transactions", json=payload, headers=self.HEADERS)
            assert response.status_code == 422
            data = response.json()
            params = {p["field"]: p["reason"] for p in data["invalid_parameters"]}
            assert "currency" in params

    def test_forbidden_sensitive_card_fields_rejected_and_redacted(
        self, client: ApiClientAdapter
    ) -> None:
        sensitive_payload = {
            "transaction_id": "txn_006",
            "merchant_id": "merchant_101",
            "payment_token": "tok_test_approved",
            "amount": 1000,
            "currency": "USD",
            "pan": "4111222233334444",
            "cvv": "999",
        }
        response = client.post("/v1/transactions", json=sensitive_payload, headers=self.HEADERS)
        assert response.status_code == 422
        assert "4111222233334444" not in response.text
        assert "999" not in response.text
        data = response.json()
        fields = [p["field"] for p in data["invalid_parameters"]]
        assert "sensitive_parameter" in fields

    def test_unknown_extra_parameters_rejected(self, client: ApiClientAdapter) -> None:
        payload = {
            "transaction_id": "txn_007",
            "merchant_id": "merchant_101",
            "payment_token": "tok_test_approved",
            "amount": 1000,
            "currency": "USD",
            "random_field": "unexpected",
        }
        response = client.post("/v1/transactions", json=payload, headers=self.HEADERS)
        assert response.status_code == 422
        data = response.json()
        fields = [p["field"] for p in data["invalid_parameters"]]
        assert "unknown_parameter" in fields

    def test_malformed_json_body_returns_http_400(self, client: ApiClientAdapter) -> None:
        response = client.post(
            "/v1/transactions",
            content=b"{not valid json",
            headers={"Content-Type": "application/json", "X-Correlation-ID": "corr_valid"},
        )
        assert response.status_code == 400
        assert response.headers["Content-Type"] == PROBLEM_MEDIA_TYPE
        data = response.json()
        assert data["type"] == "urn:problem-type:malformed-request"


class TestEmulatorScenariosEndToEnd:
    """Verify end-to-end orchestration against the in-process emulator."""

    HEADERS = {"X-Correlation-ID": "corr_e2e_test_99"}

    def test_approved_scenario_returns_http_201_authorized(
        self, client: ApiClientAdapter
    ) -> None:
        payload = {
            "transaction_id": "txn_e2e_approved",
            "merchant_id": "merchant_101",
            "payment_token": "tok_test_approved",
            "amount": 4200,
            "currency": "USD",
        }
        response = client.post("/v1/transactions", json=payload, headers=self.HEADERS)
        assert response.status_code == 201
        assert response.headers["X-Correlation-ID"] == self.HEADERS["X-Correlation-ID"]

        data = response.json()
        assert data["transaction_id"] == "txn_e2e_approved"
        assert data["status"] == "AUTHORIZED"
        assert data["version"] == 1
        assert data["network_reference"].startswith("net_")
        assert data["authorization_code"] == "AUTH123"
        assert "payment_token" not in data

    def test_declined_scenario_returns_http_201_declined(
        self, client: ApiClientAdapter
    ) -> None:
        payload = {
            "transaction_id": "txn_e2e_declined",
            "merchant_id": "merchant_101",
            "payment_token": "tok_test_declined",
            "amount": 4200,
            "currency": "USD",
        }
        response = client.post("/v1/transactions", json=payload, headers=self.HEADERS)
        assert response.status_code == 201
        assert response.headers["X-Correlation-ID"] == self.HEADERS["X-Correlation-ID"]

        data = response.json()
        assert data["transaction_id"] == "txn_e2e_declined"
        assert data["status"] == "DECLINED"
        assert data["version"] == 1
        assert data["decline_code"] == "INSUFFICIENT_FUNDS"
        assert "payment_token" not in data

    def test_timeout_scenario_returns_http_202_pending_reconciliation(
        self, emulator_app: FastAPI, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # Create an adapter with a client timeout shorter than emulator sleep
        settings = EmulatorSettings(timeout_delay_seconds=0.2)
        local_emulator = create_emulator_app(settings=settings)
        emulator_client = AsyncClient(
            transport=ASGITransport(app=local_emulator),
            base_url="http://emulator.test",
            timeout=0.02,  # Timeout before emulator finishes
        )
        app = create_transaction_app(
            http_client=emulator_client,
            emulator_base_url="http://emulator.test",
            network_timeout_seconds=0.02,
            session_factory=session_factory,
        )
        local_client = ApiClientAdapter(app)

        payload = {
            "transaction_id": "txn_e2e_timeout",
            "merchant_id": "merchant_101",
            "payment_token": "tok_test_timeout",
            "amount": 4200,
            "currency": "USD",
        }
        try:
            response = local_client.post("/v1/transactions", json=payload, headers=self.HEADERS)
            assert response.status_code == 202
            assert response.headers["X-Correlation-ID"] == self.HEADERS["X-Correlation-ID"]

            data = response.json()
            assert data["transaction_id"] == "txn_e2e_timeout"
            assert data["status"] == "PENDING_RECONCILIATION"
            assert data["version"] == 1
            assert data["pending_operation"] == "AUTHORIZATION"
        finally:
            asyncio.run(emulator_client.aclose())

    def test_malformed_scenario_returns_http_202_pending_reconciliation(
        self, client: ApiClientAdapter
    ) -> None:
        payload = {
            "transaction_id": "txn_e2e_malformed",
            "merchant_id": "merchant_101",
            "payment_token": "tok_test_malformed",
            "amount": 4200,
            "currency": "USD",
        }
        response = client.post("/v1/transactions", json=payload, headers=self.HEADERS)
        assert response.status_code == 202
        assert response.headers["X-Correlation-ID"] == self.HEADERS["X-Correlation-ID"]

        data = response.json()
        assert data["transaction_id"] == "txn_e2e_malformed"
        assert data["status"] == "PENDING_RECONCILIATION"
        assert data["version"] == 1
        assert data["pending_operation"] == "AUTHORIZATION"

    def test_rate_limited_scenario_returns_http_503(
        self, client: ApiClientAdapter
    ) -> None:
        payload = {
            "transaction_id": "txn_e2e_rate_limited",
            "merchant_id": "merchant_101",
            "payment_token": "tok_test_rate_limited",
            "amount": 4200,
            "currency": "USD",
        }
        response = client.post("/v1/transactions", json=payload, headers=self.HEADERS)
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "1"
        assert response.headers["Content-Type"] == PROBLEM_MEDIA_TYPE
        assert response.headers["X-Correlation-ID"] == self.HEADERS["X-Correlation-ID"]

        data = response.json()
        assert data["type"] == "urn:problem-type:provider-rate-limited"
        assert data["status"] == 503


class TestDownstreamErrorsSanitization:
    """Verify handling and problem details sanitization for downstream errors."""

    HEADERS = {"X-Correlation-ID": "corr_downstream_err"}
    VALID_BODY = {
        "transaction_id": "txn_err_001",
        "merchant_id": "merchant_101",
        "payment_token": "tok_test_approved",
        "amount": 5000,
        "currency": "USD",
    }

    def test_downstream_rejected_error_returns_http_502(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        failing_port = MockFailingPort(
            PaymentNetworkRejectedError("Internal provider validation error: bad secret")
        )
        app = create_transaction_app(
            payment_network=failing_port, session_factory=session_factory
        )
        client = ApiClientAdapter(app)

        response = client.post("/v1/transactions", json=self.VALID_BODY, headers=self.HEADERS)
        assert response.status_code == 502
        assert response.headers["Content-Type"] == PROBLEM_MEDIA_TYPE
        assert response.headers["X-Correlation-ID"] == self.HEADERS["X-Correlation-ID"]

        data = response.json()
        assert data["type"] == "urn:problem-type:downstream-rejected"
        assert data["status"] == 502
        # Ensure internal provider rejection detail is NOT exposed
        assert "bad secret" not in response.text

    def test_downstream_unavailable_error_returns_http_202(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        failing_port = MockFailingPort(
            PaymentNetworkUnavailableError("Internal downstream gateway crash 500")
        )
        app = create_transaction_app(
            payment_network=failing_port, session_factory=session_factory
        )
        client = ApiClientAdapter(app)

        response = client.post("/v1/transactions", json=self.VALID_BODY, headers=self.HEADERS)
        assert response.status_code == 202
        assert response.headers["X-Correlation-ID"] == self.HEADERS["X-Correlation-ID"]

        data = response.json()
        assert data["transaction_id"] == self.VALID_BODY["transaction_id"]
        assert data["status"] == "PENDING_RECONCILIATION"
        assert data["version"] == 1
        assert data["pending_operation"] == "AUTHORIZATION"

    def test_upstream_http_408_produces_http_202_pending_reconciliation(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        def handler(request: Request) -> Response:
            return Response(status_code=408, content=b"Request Timeout")

        mock_client = AsyncClient(
            transport=MockTransport(handler), base_url="http://emulator.test"
        )
        app = create_transaction_app(
            http_client=mock_client,
            emulator_base_url="http://emulator.test",
            session_factory=session_factory,
        )
        client = ApiClientAdapter(app)

        try:
            response = client.post(
                "/v1/transactions", json=self.VALID_BODY, headers=self.HEADERS
            )
            assert response.status_code == 202
            assert response.headers["X-Correlation-ID"] == self.HEADERS["X-Correlation-ID"]

            data = response.json()
            assert data["transaction_id"] == self.VALID_BODY["transaction_id"]
            assert data["status"] == "PENDING_RECONCILIATION"
            assert data["version"] == 1
            assert data["pending_operation"] == "AUTHORIZATION"
        finally:
            asyncio.run(mock_client.aclose())
