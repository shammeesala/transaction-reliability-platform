"""FastAPI application factory and routes for the transaction API."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse, Response

from transaction_platform.adapters.http_payment_network import HttpPaymentNetworkAdapter
from transaction_platform.api.errors import (
    downstream_rejected_handler,
    missing_correlation_id_handler,
    rate_limit_exceeded_handler,
    validation_exception_handler,
)
from transaction_platform.api.schemas import (
    AuthorizedTransactionResponse,
    CreateTransactionRequest,
    DeclinedTransactionResponse,
    PendingReconciliationTransactionResponse,
)
from transaction_platform.application.authorize_transaction import (
    AuthorizeTransactionCommand,
    AuthorizeTransactionService,
    DownstreamRateLimitExceededError,
    DownstreamRejectedError,
)
from transaction_platform.common.correlation import (
    CORRELATION_ID_REGEX,
    MissingCorrelationIdError,
    require_correlation_id,
)
from transaction_platform.domain.transaction import TransactionStatus
from transaction_platform.ports.payment_network import PaymentNetworkPort


def get_payment_network() -> PaymentNetworkPort:
    """Dependency provider placeholder for PaymentNetworkPort."""
    raise RuntimeError("PaymentNetworkPort dependency is not configured.")


def get_service(
    payment_network: Annotated[PaymentNetworkPort, Depends(get_payment_network)],
) -> AuthorizeTransactionService:
    """Dependency provider returning AuthorizeTransactionService."""
    return AuthorizeTransactionService(payment_network=payment_network)


def create_app(
    payment_network: PaymentNetworkPort | None = None,
    http_client: httpx.AsyncClient | None = None,
    emulator_base_url: str = "http://localhost:8001",
    network_timeout_seconds: float = 5.0,
) -> FastAPI:
    """Create and configure the main transaction FastAPI application."""
    internal_client: httpx.AsyncClient | None = None

    if payment_network is not None:
        resolved_network: PaymentNetworkPort = payment_network
    elif http_client is not None:
        resolved_network = HttpPaymentNetworkAdapter(
            base_url=emulator_base_url,
            client=http_client,
            timeout_seconds=network_timeout_seconds,
        )
    else:
        internal_client = httpx.AsyncClient()
        resolved_network = HttpPaymentNetworkAdapter(
            base_url=emulator_base_url,
            client=internal_client,
            timeout_seconds=network_timeout_seconds,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if internal_client is not None:
            await internal_client.aclose()

    app = FastAPI(
        title="Transaction Reliability Platform API",
        description="Main transaction orchestration API service.",
        lifespan=lifespan,
    )

    app.dependency_overrides[get_payment_network] = lambda: resolved_network

    # Register RFC 7807 problem exception handlers
    app.add_exception_handler(MissingCorrelationIdError, missing_correlation_id_handler)
    app.add_exception_handler(DownstreamRateLimitExceededError, rate_limit_exceeded_handler)
    app.add_exception_handler(DownstreamRejectedError, downstream_rejected_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)

    @app.middleware("http")
    async def correlation_id_middleware(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        raw_cid = request.headers.get("X-Correlation-ID")
        if raw_cid and CORRELATION_ID_REGEX.match(raw_cid):
            response.headers["X-Correlation-ID"] = raw_cid
        return response

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        """Liveness check endpoint."""
        return {"status": "ok"}

    @app.post(
        "/v1/transactions",
        response_model=None,
        responses={
            201: {"description": "Transaction successfully authorized or declined"},
            202: {"description": "Transaction pending reconciliation"},
            400: {"description": "Malformed request or missing correlation ID"},
            422: {"description": "Validation error"},
            502: {"description": "Downstream payment network rejected request"},
            503: {"description": "Downstream payment network rate limited"},
        },
    )
    async def create_transaction(
        request: CreateTransactionRequest,
        correlation_id: Annotated[str, Depends(require_correlation_id)],
        service: Annotated[AuthorizeTransactionService, Depends(get_service)],
    ) -> Response:
        """Process inbound payment transaction authorization."""
        command = AuthorizeTransactionCommand(
            transaction_id=request.transaction_id,
            merchant_id=request.merchant_id,
            payment_token=request.payment_token,
            amount=request.amount,
            currency=request.currency,
            correlation_id=correlation_id,
        )
        result = await service.execute(command)

        if result.transaction.status == TransactionStatus.AUTHORIZED:
            authorized_body = AuthorizedTransactionResponse(
                transaction_id=result.transaction.transaction_id,
                status="AUTHORIZED",
                version=result.transaction.version,
                network_reference=result.network_reference or "",
                authorization_code=result.authorization_code or "",
            )
            return JSONResponse(status_code=201, content=authorized_body.model_dump())

        if result.transaction.status == TransactionStatus.DECLINED:
            declined_body = DeclinedTransactionResponse(
                transaction_id=result.transaction.transaction_id,
                status="DECLINED",
                version=result.transaction.version,
                decline_code=result.decline_code or "",
            )
            return JSONResponse(status_code=201, content=declined_body.model_dump())

        pending_body = PendingReconciliationTransactionResponse(
            transaction_id=result.transaction.transaction_id,
            status="PENDING_RECONCILIATION",
            version=result.transaction.version,
            pending_operation="AUTHORIZATION",
        )
        return JSONResponse(status_code=202, content=pending_body.model_dump())

    return app


app = create_app()
