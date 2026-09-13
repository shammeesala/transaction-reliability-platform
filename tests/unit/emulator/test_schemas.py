"""Unit tests for payment network emulator schemas and input boundaries."""

import pytest
from pydantic import ValidationError

from transaction_platform.emulator.schemas import (
    AuthorizationRequest,
    AuthorizedResponse,
    DeclinedResponse,
    InvalidParameter,
    ProblemDetail,
)
from transaction_platform.emulator.settings import EmulatorSettings


class TestAuthorizationRequestValidation:
    """Validate input boundaries for AuthorizationRequest."""

    def test_valid_request_constructs_successfully(self) -> None:
        req = AuthorizationRequest(
            request_id="req_123",
            merchant_id="merchant_101",
            payment_token="tok_test_approved",
            amount=12500,
            currency="USD",
        )
        assert req.request_id == "req_123"
        assert req.merchant_id == "merchant_101"
        assert req.payment_token == "tok_test_approved"
        assert req.amount == 12500
        assert req.currency == "USD"

    def test_string_fields_are_stripped(self) -> None:
        req = AuthorizationRequest(
            request_id="  req_whitespace  ",
            merchant_id="  merchant_1  ",
            payment_token="  tok_test_approved  ",
            amount=500,
            currency="USD",
        )
        assert req.request_id == "req_whitespace"
        assert req.merchant_id == "merchant_1"
        assert req.payment_token == "tok_test_approved"

    @pytest.mark.parametrize("field", ["request_id", "merchant_id", "payment_token"])
    @pytest.mark.parametrize("invalid_value", ["", "   ", "\t\n  "])
    def test_blank_string_fields_are_rejected(self, field: str, invalid_value: str) -> None:
        data = {
            "request_id": "req_123",
            "merchant_id": "merchant_101",
            "payment_token": "tok_test_approved",
            "amount": 100,
            "currency": "USD",
        }
        data[field] = invalid_value
        with pytest.raises(ValidationError) as exc_info:
            AuthorizationRequest.model_validate(data)
        assert any(err["loc"] == (field,) for err in exc_info.value.errors())

    @pytest.mark.parametrize("field", ["request_id", "merchant_id", "payment_token"])
    def test_string_fields_exceeding_max_length_are_rejected(self, field: str) -> None:
        data = {
            "request_id": "req_123",
            "merchant_id": "merchant_101",
            "payment_token": "tok_test_approved",
            "amount": 100,
            "currency": "USD",
        }
        data[field] = "x" * 129
        with pytest.raises(ValidationError) as exc_info:
            AuthorizationRequest.model_validate(data)
        assert any(err["loc"] == (field,) for err in exc_info.value.errors())

    @pytest.mark.parametrize("field", ["request_id", "merchant_id", "payment_token"])
    @pytest.mark.parametrize("non_string_value", [123, True, None, [], {}])
    def test_non_string_types_are_rejected(self, field: str, non_string_value: object) -> None:
        data = {
            "request_id": "req_123",
            "merchant_id": "merchant_101",
            "payment_token": "tok_test_approved",
            "amount": 100,
            "currency": "USD",
        }
        data[field] = non_string_value
        with pytest.raises(ValidationError) as exc_info:
            AuthorizationRequest.model_validate(data)
        assert any(err["loc"] == (field,) for err in exc_info.value.errors())

    @pytest.mark.parametrize("valid_currency", ["USD", "EUR", "GBP", "JPY", "CAD"])
    def test_valid_iso_4217_currency_format_accepted(self, valid_currency: str) -> None:
        req = AuthorizationRequest(
            request_id="req_123",
            merchant_id="merchant_101",
            payment_token="tok_test_approved",
            amount=1000,
            currency=valid_currency,
        )
        assert req.currency == valid_currency

    @pytest.mark.parametrize("invalid_currency", ["usd", "Usd", "US", "USDT", "123", "U$D", ""])
    def test_invalid_currency_format_rejected(self, invalid_currency: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AuthorizationRequest(
                request_id="req_123",
                merchant_id="merchant_101",
                payment_token="tok_test_approved",
                amount=1000,
                currency=invalid_currency,
            )
        assert any(err["loc"] == ("currency",) for err in exc_info.value.errors())

    @pytest.mark.parametrize("boolean_amount", [True, False])
    def test_boolean_amount_strictly_rejected(self, boolean_amount: bool) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AuthorizationRequest(
                request_id="req_123",
                merchant_id="merchant_101",
                payment_token="tok_test_approved",
                amount=boolean_amount,
                currency="USD",
            )
        assert any(err["loc"] == ("amount",) for err in exc_info.value.errors())

    @pytest.mark.parametrize("non_positive_amount", [0, -1, -500])
    def test_non_positive_amount_rejected(self, non_positive_amount: int) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AuthorizationRequest(
                request_id="req_123",
                merchant_id="merchant_101",
                payment_token="tok_test_approved",
                amount=non_positive_amount,
                currency="USD",
            )
        assert any(err["loc"] == ("amount",) for err in exc_info.value.errors())

    @pytest.mark.parametrize("invalid_type_amount", [10.5, "100", None, [], {}])
    def test_invalid_type_amount_rejected(self, invalid_type_amount: object) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AuthorizationRequest(
                request_id="req_123",
                merchant_id="merchant_101",
                payment_token="tok_test_approved",
                amount=invalid_type_amount,  # type: ignore[arg-type]
                currency="USD",
            )
        assert any(err["loc"] == ("amount",) for err in exc_info.value.errors())

    def test_extra_fields_forbidden(self) -> None:
        data = {
            "request_id": "req_123",
            "merchant_id": "merchant_101",
            "payment_token": "tok_test_approved",
            "amount": 100,
            "currency": "USD",
            "cvv": "123",
            "pan": "4111111111111111",
        }
        with pytest.raises(ValidationError) as exc_info:
            AuthorizationRequest.model_validate(data)
        errors = exc_info.value.errors()
        error_fields = [err["loc"][0] for err in errors]
        assert "cvv" in error_fields
        assert "pan" in error_fields


class TestResponseAndProblemSchemas:
    """Verify response model defaults and RFC 7807 problem details."""

    def test_authorized_response_defaults(self) -> None:
        resp = AuthorizedResponse(network_reference="net_abcdef0123456789")
        assert resp.outcome == "AUTHORIZED"
        assert resp.authorization_code == "AUTH123"
        assert resp.network_reference == "net_abcdef0123456789"

    def test_declined_response_defaults(self) -> None:
        resp = DeclinedResponse()
        assert resp.outcome == "DECLINED"
        assert resp.decline_code == "INSUFFICIENT_FUNDS"

    def test_problem_detail_serialization(self) -> None:
        problem = ProblemDetail(
            type="urn:problem-type:validation-error",
            title="Validation error",
            status=422,
            detail="Request body failed validation.",
            invalid_parameters=[
                InvalidParameter(field="amount", reason="amount must be greater than zero")
            ],
        )
        data = problem.model_dump(exclude_none=True)
        assert data["type"] == "urn:problem-type:validation-error"
        assert data["status"] == 422
        assert len(data["invalid_parameters"]) == 1
        assert data["invalid_parameters"][0]["field"] == "amount"


class TestEmulatorSettingsValidation:
    """Verify bounds and safety validation for EmulatorSettings."""

    def test_default_timeout_delay_is_valid(self) -> None:
        settings = EmulatorSettings()
        assert settings.timeout_delay_seconds == 0.01

    @pytest.mark.parametrize("valid_delay", [0.0, 0.01, 15.0, 30.0])
    def test_valid_timeout_delays_accepted(self, valid_delay: float) -> None:
        settings = EmulatorSettings(timeout_delay_seconds=valid_delay)
        assert settings.timeout_delay_seconds == valid_delay

    @pytest.mark.parametrize("negative_delay", [-0.01, -1.0, -100.0])
    def test_negative_timeout_delay_rejected(self, negative_delay: float) -> None:
        with pytest.raises(ValidationError):
            EmulatorSettings(timeout_delay_seconds=negative_delay)

    @pytest.mark.parametrize("excessive_delay", [30.01, 31.0, 1000.0])
    def test_timeout_delay_exceeding_max_rejected(self, excessive_delay: float) -> None:
        with pytest.raises(ValidationError):
            EmulatorSettings(timeout_delay_seconds=excessive_delay)

    @pytest.mark.parametrize(
        "non_finite_delay",
        [float("nan"), float("inf"), float("-inf")],
    )
    def test_non_finite_timeout_delay_rejected(self, non_finite_delay: float) -> None:
        with pytest.raises(ValidationError):
            EmulatorSettings(timeout_delay_seconds=non_finite_delay)
