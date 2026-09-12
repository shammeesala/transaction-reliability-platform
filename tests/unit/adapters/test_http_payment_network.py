"""Unit tests for HttpPaymentNetworkAdapter using httpx.MockTransport."""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from transaction_platform.adapters.http_payment_network import (
    HttpPaymentNetworkAdapter,
    parse_retry_after,
)
from transaction_platform.ports.payment_network import (
    PaymentNetworkAuthorizationRequest,
    PaymentNetworkAuthorizedResult,
    PaymentNetworkDeclinedResult,
    PaymentNetworkInvalidResponseError,
    PaymentNetworkRateLimitError,
    PaymentNetworkRejectedError,
    PaymentNetworkTimeoutError,
    PaymentNetworkUnavailableError,
)


def make_request(
    transaction_id: str = "txn_adapt_1",
    merchant_id: str = "merchant_1",
    payment_token: str = "tok_test_approved",
    amount: int = 1500,
    currency: str = "USD",
    correlation_id: str = "corr_adapt_1",
) -> PaymentNetworkAuthorizationRequest:
    return PaymentNetworkAuthorizationRequest(
        transaction_id=transaction_id,
        merchant_id=merchant_id,
        payment_token=payment_token,
        amount=amount,
        currency=currency,
        correlation_id=correlation_id,
    )


async def execute_adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    req: PaymentNetworkAuthorizationRequest | None = None,
    base_url: str = "http://emulator.test",
) -> PaymentNetworkAuthorizedResult | PaymentNetworkDeclinedResult:
    """Run adapter in an async context manager ensuring AsyncClient is always closed."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = HttpPaymentNetworkAdapter(base_url=base_url, client=client)
        return await adapter.authorize(req or make_request())


class TestParseRetryAfter:
    """Verify safe parsing and clamping of Retry-After header values."""

    def test_parses_valid_positive_integers(self) -> None:
        assert parse_retry_after("1") == 1
        assert parse_retry_after("5") == 5
        assert parse_retry_after("60") == 60

    def test_defaults_on_missing_or_empty_values(self) -> None:
        assert parse_retry_after(None) == 1
        assert parse_retry_after("") == 1
        assert parse_retry_after("   ") == 1

    def test_defaults_on_non_numeric_values(self) -> None:
        assert parse_retry_after("one") == 1
        assert parse_retry_after("12.5") == 1
        assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") == 1

    def test_rejects_negative_values(self) -> None:
        assert parse_retry_after("-1") == 1
        assert parse_retry_after("-100") == 1

    def test_clamps_to_max_value(self) -> None:
        assert parse_retry_after("61") == 60
        assert parse_retry_after("9999") == 60

    def test_clamps_to_min_value_for_zero(self) -> None:
        assert parse_retry_after("0") == 1


class TestHttpPaymentNetworkAdapter:
    """Verify HTTP adapter serialization, deserialization, and error translation."""

    def test_authorized_response_mapping(self) -> None:
        recorded_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            recorded_requests.append(request)
            return httpx.Response(
                status_code=200,
                json={
                    "outcome": "AUTHORIZED",
                    "network_reference": "net_abc_12345",
                    "authorization_code": "AUTH123",
                },
            )

        req = make_request()
        result = asyncio.run(execute_adapter(handler, req=req))

        assert isinstance(result, PaymentNetworkAuthorizedResult)
        assert result.network_reference == "net_abc_12345"
        assert result.authorization_code == "AUTH123"

        assert len(recorded_requests) == 1
        sent = recorded_requests[0]
        assert sent.url == "http://emulator.test/v1/authorizations"
        assert sent.headers["X-Correlation-ID"] == req.correlation_id

        body = json.loads(sent.content)
        assert body["request_id"] == req.transaction_id
        assert body["merchant_id"] == req.merchant_id
        assert body["payment_token"] == req.payment_token
        assert body["amount"] == req.amount
        assert body["currency"] == req.currency

    def test_declined_response_mapping(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={
                    "outcome": "DECLINED",
                    "decline_code": "INSUFFICIENT_FUNDS",
                },
            )

        result = asyncio.run(execute_adapter(handler))

        assert isinstance(result, PaymentNetworkDeclinedResult)
        assert result.decline_code == "INSUFFICIENT_FUNDS"

    def test_malformed_json_response_raises_invalid_response_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                content=b"{not valid json",
                headers={"Content-Type": "application/json"},
            )

        with pytest.raises(PaymentNetworkInvalidResponseError):
            asyncio.run(execute_adapter(handler))

    def test_non_dict_json_response_raises_invalid_response_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=200, json=["unexpected", "array"])

        with pytest.raises(PaymentNetworkInvalidResponseError):
            asyncio.run(execute_adapter(handler))

    @pytest.mark.parametrize(
        "invalid_payload",
        [
            {"outcome": "AUTHORIZED"},  # Missing network_reference and auth_code
            {"outcome": "AUTHORIZED", "network_reference": "net_123"},  # Missing auth_code
            {"outcome": "AUTHORIZED", "network_reference": "   ", "authorization_code": "AUTH1"},
            {"outcome": "AUTHORIZED", "network_reference": "", "authorization_code": "AUTH1"},
            {"outcome": "AUTHORIZED", "network_reference": "net_123", "authorization_code": ""},
            {"outcome": "AUTHORIZED", "network_reference": "net_123", "authorization_code": "   "},
            {"outcome": "DECLINED"},  # Missing decline_code
            {"outcome": "DECLINED", "decline_code": ""},  # Empty decline_code
            {"outcome": "DECLINED", "decline_code": "   "},  # Whitespace decline_code
            {"outcome": "UNKNOWN"},  # Unrecognized outcome
        ],
    )
    def test_contract_invalid_payloads_raise_invalid_response_error(
        self, invalid_payload: dict[str, Any]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=200, json=invalid_payload)

        with pytest.raises(PaymentNetworkInvalidResponseError):
            asyncio.run(execute_adapter(handler))

    def test_http_429_raises_rate_limit_error_with_parsed_retry_after(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=429, headers={"Retry-After": "3"})

        with pytest.raises(PaymentNetworkRateLimitError) as exc_info:
            asyncio.run(execute_adapter(handler))

        assert exc_info.value.retry_after == 3

    def test_http_408_raises_payment_network_timeout_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=408, content=b"Request Timeout")

        with pytest.raises(PaymentNetworkTimeoutError):
            asyncio.run(execute_adapter(handler))

    @pytest.mark.parametrize("status_code", [400, 422, 404, 403])
    def test_http_4xx_raises_payment_network_rejected_error(self, status_code: int) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=status_code,
                json={"error": "provider rejected request body"},
            )

        with pytest.raises(PaymentNetworkRejectedError):
            asyncio.run(execute_adapter(handler))

    @pytest.mark.parametrize("status_code", [500, 502, 503, 504])
    def test_http_5xx_raises_payment_network_unavailable_error(self, status_code: int) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=status_code, content=b"Server error")

        with pytest.raises(PaymentNetworkUnavailableError):
            asyncio.run(execute_adapter(handler))

    def test_read_timeout_raises_payment_network_timeout_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("Read timed out")

        with pytest.raises(PaymentNetworkTimeoutError):
            asyncio.run(execute_adapter(handler))

    def test_connect_error_raises_payment_network_unavailable_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        with pytest.raises(PaymentNetworkUnavailableError):
            asyncio.run(execute_adapter(handler))

    def test_remote_protocol_error_raises_payment_network_unavailable_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.RemoteProtocolError("Server disconnected abruptly")

        with pytest.raises(PaymentNetworkUnavailableError):
            asyncio.run(execute_adapter(handler))
