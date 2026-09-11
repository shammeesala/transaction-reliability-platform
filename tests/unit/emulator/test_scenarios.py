"""Unit tests for payment network emulator scenarios and deterministic reference derivation."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from starlette.responses import Response

from transaction_platform.emulator.errors import (
    RateLimitedError,
    UnsupportedTokenError,
)
from transaction_platform.emulator.scenarios import (
    TOKEN_APPROVED,
    TOKEN_DECLINED,
    TOKEN_MALFORMED,
    TOKEN_RATE_LIMITED,
    TOKEN_TIMEOUT,
    derive_network_reference,
    execute_authorization_scenario,
)
from transaction_platform.emulator.schemas import (
    AuthorizationRequest,
    AuthorizedResponse,
    DeclinedResponse,
)


class TestDeterministicNetworkReference:
    """Verify deterministic reference generation properties."""

    def test_same_request_id_produces_identical_reference(self) -> None:
        ref1 = derive_network_reference("req_abc_123")
        ref2 = derive_network_reference("req_abc_123")
        assert ref1 == ref2
        assert ref1.startswith("net_")
        assert len(ref1) == 20  # "net_" (4) + 16 hex chars

    def test_different_request_ids_produce_different_references(self) -> None:
        ref1 = derive_network_reference("req_001")
        ref2 = derive_network_reference("req_002")
        assert ref1 != ref2


class TestScenarioExecution:
    """Verify scenario selection and execution in scenarios.py."""

    @pytest.fixture
    def sleeper_mock(self) -> AsyncMock:
        return AsyncMock()

    def test_approved_scenario_returns_authorized_response(
        self, sleeper_mock: AsyncMock
    ) -> None:
        req = AuthorizationRequest(
            request_id="req_app_1",
            merchant_id="merchant_101",
            payment_token=TOKEN_APPROVED,
            amount=5000,
            currency="USD",
        )
        result = asyncio.run(
            execute_authorization_scenario(
                request=req,
                timeout_delay=0.01,
                sleeper=sleeper_mock,
            )
        )
        assert isinstance(result, AuthorizedResponse)
        assert result.outcome == "AUTHORIZED"
        assert result.authorization_code == "AUTH123"
        assert result.network_reference == derive_network_reference("req_app_1")
        sleeper_mock.assert_not_awaited()

    def test_declined_scenario_returns_declined_response(
        self, sleeper_mock: AsyncMock
    ) -> None:
        req = AuthorizationRequest(
            request_id="req_dec_1",
            merchant_id="merchant_101",
            payment_token=TOKEN_DECLINED,
            amount=5000,
            currency="USD",
        )
        result = asyncio.run(
            execute_authorization_scenario(
                request=req,
                timeout_delay=0.01,
                sleeper=sleeper_mock,
            )
        )
        assert isinstance(result, DeclinedResponse)
        assert result.outcome == "DECLINED"
        assert result.decline_code == "INSUFFICIENT_FUNDS"
        sleeper_mock.assert_not_awaited()

    def test_rate_limited_scenario_raises_rate_limited_error(
        self, sleeper_mock: AsyncMock
    ) -> None:
        req = AuthorizationRequest(
            request_id="req_rl_1",
            merchant_id="merchant_101",
            payment_token=TOKEN_RATE_LIMITED,
            amount=5000,
            currency="USD",
        )
        with pytest.raises(RateLimitedError):
            asyncio.run(
                execute_authorization_scenario(
                    request=req,
                    timeout_delay=0.01,
                    sleeper=sleeper_mock,
                )
            )
        sleeper_mock.assert_not_awaited()

    def test_malformed_scenario_returns_raw_unparseable_response(
        self, sleeper_mock: AsyncMock
    ) -> None:
        req = AuthorizationRequest(
            request_id="req_mal_1",
            merchant_id="merchant_101",
            payment_token=TOKEN_MALFORMED,
            amount=5000,
            currency="USD",
        )
        result = asyncio.run(
            execute_authorization_scenario(
                request=req,
                timeout_delay=0.01,
                sleeper=sleeper_mock,
            )
        )
        assert isinstance(result, Response)
        assert result.status_code == 200
        assert result.media_type == "application/json"
        # Verify the content is deliberately unparseable JSON
        with pytest.raises(json.JSONDecodeError):
            json.loads(bytes(result.body).decode("utf-8"))
        sleeper_mock.assert_not_awaited()

    def test_timeout_scenario_awaits_injected_sleeper(
        self, sleeper_mock: AsyncMock
    ) -> None:
        req = AuthorizationRequest(
            request_id="req_to_1",
            merchant_id="merchant_101",
            payment_token=TOKEN_TIMEOUT,
            amount=5000,
            currency="USD",
        )
        result = asyncio.run(
            execute_authorization_scenario(
                request=req,
                timeout_delay=0.05,
                sleeper=sleeper_mock,
            )
        )
        assert isinstance(result, AuthorizedResponse)
        assert result.outcome == "AUTHORIZED"
        assert result.network_reference == derive_network_reference("req_to_1")
        sleeper_mock.assert_awaited_once_with(0.05)

    def test_unsupported_token_raises_unsupported_token_error(
        self, sleeper_mock: AsyncMock
    ) -> None:
        req = AuthorizationRequest(
            request_id="req_unk_1",
            merchant_id="merchant_101",
            payment_token="tok_test_unrecognized_value",
            amount=5000,
            currency="USD",
        )
        with pytest.raises(UnsupportedTokenError):
            asyncio.run(
                execute_authorization_scenario(
                    request=req,
                    timeout_delay=0.01,
                    sleeper=sleeper_mock,
                )
            )
        sleeper_mock.assert_not_awaited()
