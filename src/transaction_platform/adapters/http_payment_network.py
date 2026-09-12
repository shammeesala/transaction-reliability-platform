"""HTTPX-based secondary adapter implementing the payment network port."""

import asyncio
import json
from typing import Any

import httpx

from transaction_platform.ports.payment_network import (
    PaymentNetworkAuthorizationRequest,
    PaymentNetworkAuthorizedResult,
    PaymentNetworkDeclinedResult,
    PaymentNetworkInvalidResponseError,
    PaymentNetworkPort,
    PaymentNetworkRateLimitError,
    PaymentNetworkRejectedError,
    PaymentNetworkTimeoutError,
    PaymentNetworkUnavailableError,
)


def parse_retry_after(
    header_value: str | None,
    default: int = 1,
    min_value: int = 1,
    max_value: int = 60,
) -> int:
    """Parse and sanitize a Retry-After header value safely.

    Rules:
    - Missing, blank, or non-integer values default to 1.
    - Negative values are rejected and default to 1.
    - Values are clamped to a safe maximum (e.g. 60).
    - Never reflects arbitrary upstream header text.
    """
    if not header_value:
        return default
    try:
        parsed = int(header_value.strip())
        if parsed < 0:
            return default
        return max(min_value, min(parsed, max_value))
    except (ValueError, TypeError):
        return default


class HttpPaymentNetworkAdapter(PaymentNetworkPort):
    """Secondary adapter that communicates with the payment network emulator over HTTP."""

    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client
        self.timeout_seconds = timeout_seconds

    async def authorize(
        self, request: PaymentNetworkAuthorizationRequest
    ) -> PaymentNetworkAuthorizedResult | PaymentNetworkDeclinedResult:
        """Call downstream emulator /v1/authorizations and map responses to typed results."""
        endpoint = f"{self.base_url}/v1/authorizations"
        payload = {
            "request_id": request.transaction_id,
            "merchant_id": request.merchant_id,
            "payment_token": request.payment_token,
            "amount": request.amount,
            "currency": request.currency,
        }
        headers = {"X-Correlation-ID": request.correlation_id}

        try:
            async with asyncio.timeout(self.timeout_seconds):
                response = await self.client.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise PaymentNetworkTimeoutError(
                "Payment network authorization request timed out."
            ) from exc
        except httpx.TransportError as exc:
            raise PaymentNetworkUnavailableError(
                "Payment network transport unavailable or connection dropped."
            ) from exc

        return self._handle_response(response)

    def _handle_response(
        self, response: httpx.Response
    ) -> PaymentNetworkAuthorizedResult | PaymentNetworkDeclinedResult:
        status = response.status_code

        # 1. HTTP 200: evaluate contract payload
        if status == 200:
            try:
                data: dict[str, Any] = response.json()
            except (json.JSONDecodeError, ValueError) as exc:
                raise PaymentNetworkInvalidResponseError(
                    "Payment network returned invalid or malformed JSON payload."
                ) from exc

            if not isinstance(data, dict):
                raise PaymentNetworkInvalidResponseError(
                    "Payment network response body must be a JSON object."
                )

            outcome = data.get("outcome")
            if outcome == "AUTHORIZED":
                net_ref = data.get("network_reference")
                auth_code = data.get("authorization_code")
                if (
                    isinstance(net_ref, str)
                    and net_ref.strip()
                    and isinstance(auth_code, str)
                    and auth_code.strip()
                ):
                    return PaymentNetworkAuthorizedResult(
                        network_reference=net_ref.strip(),
                        authorization_code=auth_code.strip(),
                    )
                raise PaymentNetworkInvalidResponseError(
                    "Payment network AUTHORIZED response missing required fields."
                )

            if outcome == "DECLINED":
                decline_code = data.get("decline_code")
                if isinstance(decline_code, str) and decline_code.strip():
                    return PaymentNetworkDeclinedResult(decline_code=decline_code.strip())
                raise PaymentNetworkInvalidResponseError(
                    "Payment network DECLINED response missing decline_code."
                )

            raise PaymentNetworkInvalidResponseError(
                "Payment network returned an unrecognized authorization outcome."
            )

        # 2. HTTP 429: rate limit exceeded prior to authorization
        # Note: Under the emulator contract, HTTP 429 guarantees the request was rejected
        # at the gateway before transaction processing began.
        if status == 429:
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            raise PaymentNetworkRateLimitError(retry_after=retry_after)

        # 3. HTTP 408: request timeout (ambiguous outcome treated as timeout)
        if status == 408:
            raise PaymentNetworkTimeoutError(
                "Payment network returned HTTP 408 Request Timeout."
            )

        # 4. HTTP 400/422 and other 4xx: explicit downstream request rejection
        if 400 <= status < 500:
            raise PaymentNetworkRejectedError(
                "Payment network explicitly rejected the authorization request."
            )

        # 4. HTTP 5xx: upstream server error (outcome is conservatively treated as unknown)
        if 500 <= status < 600:
            raise PaymentNetworkUnavailableError(
                "Payment network returned an internal server error."
            )

        # Fallback for unexpected status codes
        raise PaymentNetworkUnavailableError(
            "Payment network returned an unexpected HTTP response status."
        )
