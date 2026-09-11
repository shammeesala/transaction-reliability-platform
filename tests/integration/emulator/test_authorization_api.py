"""Integration tests for payment network emulator HTTP contracts and problem details."""

import asyncio
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from transaction_platform.emulator.app import create_app
from transaction_platform.emulator.scenarios import (
    TOKEN_APPROVED,
    TOKEN_DECLINED,
    TOKEN_MALFORMED,
    TOKEN_RATE_LIMITED,
    TOKEN_TIMEOUT,
    get_async_sleeper,
)
from transaction_platform.emulator.settings import EmulatorSettings


class ClientAdapter:
    """Synchronous test adapter executing requests via httpx.AsyncClient and ASGITransport."""

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
        return self._request("POST", url, **kwargs)


@pytest.fixture
def test_settings() -> EmulatorSettings:
    return EmulatorSettings(timeout_delay_seconds=0.02)


@pytest.fixture
def app(test_settings: EmulatorSettings) -> Iterator[FastAPI]:
    test_app = create_app(settings=test_settings)
    yield test_app
    test_app.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> ClientAdapter:
    return ClientAdapter(app)


class TestLivenessEndpoint:
    """Verify service health / liveness check."""

    def test_health_check_returns_ok(self, client: ClientAdapter) -> None:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestScenarioContracts:
    """Verify HTTP status codes, headers, and payloads for all simulated scenarios."""

    def test_approved_scenario_contract(self, client: ClientAdapter) -> None:
        payload = {
            "request_id": "req_app_100",
            "merchant_id": "merchant_1",
            "payment_token": TOKEN_APPROVED,
            "amount": 10000,
            "currency": "USD",
        }
        headers = {"X-Correlation-ID": "corr_app_100"}
        response = client.post("/v1/authorizations", json=payload, headers=headers)

        assert response.status_code == 200
        assert response.headers.get("X-Correlation-ID") == "corr_app_100"
        data = response.json()
        assert data["outcome"] == "AUTHORIZED"
        assert data["authorization_code"] == "AUTH123"
        assert data["network_reference"].startswith("net_")

    def test_declined_scenario_contract_is_200_handled_outcome(
        self, client: ClientAdapter
    ) -> None:
        payload = {
            "request_id": "req_dec_100",
            "merchant_id": "merchant_1",
            "payment_token": TOKEN_DECLINED,
            "amount": 10000,
            "currency": "USD",
        }
        headers = {"X-Correlation-ID": "corr_dec_100"}
        response = client.post("/v1/authorizations", json=payload, headers=headers)

        # A decline is a handled business outcome, not an HTTP 402 transport error
        assert response.status_code == 200
        assert response.headers.get("X-Correlation-ID") == "corr_dec_100"
        data = response.json()
        assert data["outcome"] == "DECLINED"
        assert data["decline_code"] == "INSUFFICIENT_FUNDS"

    def test_rate_limited_scenario_contract(self, client: ClientAdapter) -> None:
        payload = {
            "request_id": "req_rl_100",
            "merchant_id": "merchant_1",
            "payment_token": TOKEN_RATE_LIMITED,
            "amount": 10000,
            "currency": "USD",
        }
        headers = {"X-Correlation-ID": "corr_rl_100"}
        response = client.post("/v1/authorizations", json=payload, headers=headers)

        assert response.status_code == 429
        assert response.headers.get("Retry-After") == "1"
        assert response.headers.get("Content-Type") == "application/problem+json"
        assert response.headers.get("X-Correlation-ID") == "corr_rl_100"
        problem = response.json()
        assert problem["type"] == "urn:problem-type:rate-limited"
        assert problem["status"] == 429
        assert "simulated rate limit" in problem["detail"]

    def test_malformed_provider_response_contract(self, client: ClientAdapter) -> None:
        payload = {
            "request_id": "req_mal_100",
            "merchant_id": "merchant_1",
            "payment_token": TOKEN_MALFORMED,
            "amount": 10000,
            "currency": "USD",
        }
        headers = {"X-Correlation-ID": "corr_mal_100"}
        response = client.post("/v1/authorizations", json=payload, headers=headers)

        assert response.status_code == 200
        assert response.headers.get("Content-Type") == "application/json"
        assert response.headers.get("X-Correlation-ID") == "corr_mal_100"
        # Must be deliberately invalid JSON
        with pytest.raises(json.JSONDecodeError):
            json.loads(response.text)

    def test_timeout_scenario_with_injected_sleeper(
        self, app: FastAPI, client: ClientAdapter, test_settings: EmulatorSettings
    ) -> None:
        sleeper_mock = AsyncMock()
        app.dependency_overrides[get_async_sleeper] = lambda: sleeper_mock

        payload = {
            "request_id": "req_to_100",
            "merchant_id": "merchant_1",
            "payment_token": TOKEN_TIMEOUT,
            "amount": 10000,
            "currency": "USD",
        }
        headers = {"X-Correlation-ID": "corr_to_100"}
        response = client.post("/v1/authorizations", json=payload, headers=headers)

        assert response.status_code == 200
        assert response.headers.get("X-Correlation-ID") == "corr_to_100"
        data = response.json()
        assert data["outcome"] == "AUTHORIZED"
        sleeper_mock.assert_awaited_once_with(test_settings.timeout_delay_seconds)

    def test_unsupported_token_returns_sanitized_problem_without_echo(
        self, client: ClientAdapter
    ) -> None:
        unsupported_token = "tok_test_completely_unknown_value"
        payload = {
            "request_id": "req_unk_100",
            "merchant_id": "merchant_1",
            "payment_token": unsupported_token,
            "amount": 10000,
            "currency": "USD",
        }
        headers = {"X-Correlation-ID": "corr_unk_100"}
        response = client.post("/v1/authorizations", json=payload, headers=headers)

        assert response.status_code == 422
        assert response.headers.get("Content-Type") == "application/problem+json"
        assert response.headers.get("X-Correlation-ID") == "corr_unk_100"
        problem = response.json()
        assert problem["type"] == "urn:problem-type:unsupported-token"
        assert problem["status"] == 422
        # Crucial security verification: rejected token value must not be echoed
        assert unsupported_token not in response.text


class TestCorrelationIdValidation:
    """Verify validation and non-reflection of invalid X-Correlation-ID headers."""

    def test_missing_correlation_id_returns_400_problem(self, client: ClientAdapter) -> None:
        payload = {
            "request_id": "req_1",
            "merchant_id": "merchant_1",
            "payment_token": TOKEN_APPROVED,
            "amount": 1000,
            "currency": "USD",
        }
        response = client.post("/v1/authorizations", json=payload)

        assert response.status_code == 400
        assert response.headers.get("Content-Type") == "application/problem+json"
        assert "X-Correlation-ID" not in response.headers
        problem = response.json()
        assert problem["type"] == "urn:problem-type:missing-correlation-id"
        assert problem["status"] == 400

    @pytest.mark.parametrize(
        "invalid_header",
        [
            "",
            "   ",
            " corr_leading_space",
            "corr_trailing_space ",
            "corr with spaces",
            "corr_bad_char@!",
            "x" * 129,
        ],
    )
    def test_invalid_correlation_id_rejected_and_never_echoed(
        self, client: ClientAdapter, invalid_header: str
    ) -> None:
        payload = {
            "request_id": "req_1",
            "merchant_id": "merchant_1",
            "payment_token": TOKEN_APPROVED,
            "amount": 1000,
            "currency": "USD",
        }
        headers = {"X-Correlation-ID": invalid_header}
        response = client.post("/v1/authorizations", json=payload, headers=headers)

        assert response.status_code == 400
        assert response.headers.get("Content-Type") == "application/problem+json"
        assert "X-Correlation-ID" not in response.headers
        problem = response.json()
        assert problem["type"] == "urn:problem-type:missing-correlation-id"


class TestValidationAndSanitization:
    """Verify malformed request handling and sensitive data redaction."""

    def test_malformed_json_returns_400_without_echoing_body(
        self, client: ClientAdapter
    ) -> None:
        malformed_body = '{"request_id": "req_1", "broken_json": '
        headers = {
            "Content-Type": "application/json",
            "X-Correlation-ID": "corr_malformed_test",
        }
        response = client.post("/v1/authorizations", content=malformed_body, headers=headers)

        assert response.status_code == 400
        assert response.headers.get("Content-Type") == "application/problem+json"
        assert response.headers.get("X-Correlation-ID") == "corr_malformed_test"
        problem = response.json()
        assert problem["type"] == "urn:problem-type:malformed-request"
        assert problem["status"] == 400
        # Submitted invalid body must never be reflected
        assert "broken_json" not in response.text

    def test_schema_validation_error_redacts_inputs_and_forbidden_fields(
        self, client: ClientAdapter
    ) -> None:
        sensitive_cvv = "999"
        sensitive_pan = "4111222233334444"
        payload = {
            "request_id": "req_1",
            "merchant_id": "merchant_1",
            "payment_token": TOKEN_APPROVED,
            "amount": True,  # Invalid boolean amount
            "currency": "usd",  # Invalid lowercase currency
            "cvv": sensitive_cvv,  # Forbidden sensitive field
            "pan": sensitive_pan,  # Forbidden sensitive field
        }
        headers = {"X-Correlation-ID": "corr_sanitized_test"}
        response = client.post("/v1/authorizations", json=payload, headers=headers)

        assert response.status_code == 422
        assert response.headers.get("Content-Type") == "application/problem+json"
        assert response.headers.get("X-Correlation-ID") == "corr_sanitized_test"
        problem = response.json()
        assert problem["type"] == "urn:problem-type:validation-error"
        assert problem["status"] == 422

        invalid_params = problem["invalid_parameters"]
        fields = [p["field"] for p in invalid_params]
        assert "amount" in fields
        assert "currency" in fields
        assert "sensitive_parameter" in fields
        # Ensure raw forbidden field names and sensitive inputs are NEVER reflected
        assert "cvv" not in fields
        assert "pan" not in fields
        assert sensitive_cvv not in response.text
        assert sensitive_pan not in response.text
        assert "True" not in response.text

    def test_arbitrary_and_sensitive_keys_are_never_reflected(
        self, client: ClientAdapter
    ) -> None:
        sensitive_key_pan = "4111999988887777"
        sensitive_key_val = "sensitive_val_123"
        long_arbitrary_key = "a" * 300
        long_key_val = "long_val_456"
        normal_unknown_key = "custom_metadata_tag"
        normal_key_val = "normal_val_789"

        payload = {
            "request_id": "req_safe_1",
            "merchant_id": "merchant_1",
            "payment_token": TOKEN_APPROVED,
            "amount": 1000,
            "currency": "USD",
            sensitive_key_pan: sensitive_key_val,
            long_arbitrary_key: long_key_val,
            normal_unknown_key: normal_key_val,
        }
        headers = {"X-Correlation-ID": "corr_key_reflection_test"}
        response = client.post("/v1/authorizations", json=payload, headers=headers)

        assert response.status_code == 422
        assert response.headers.get("Content-Type") == "application/problem+json"
        problem = response.json()
        assert problem["type"] == "urn:problem-type:validation-error"

        invalid_params = problem["invalid_parameters"]
        fields = [p["field"] for p in invalid_params]
        assert "unknown_parameter" in fields

        # Verify NONE of the submitted keys or values appear anywhere in the response text
        assert sensitive_key_pan not in response.text
        assert sensitive_key_val not in response.text
        assert long_arbitrary_key not in response.text
        assert long_key_val not in response.text
        assert normal_unknown_key not in response.text
        assert normal_key_val not in response.text
