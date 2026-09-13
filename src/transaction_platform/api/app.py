"""FastAPI application factory and routes for the transaction API."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from starlette.responses import JSONResponse, Response

from transaction_platform.adapters.http_payment_network import HttpPaymentNetworkAdapter
from transaction_platform.api.errors import (
    downstream_rejected_handler,
    missing_correlation_id_handler,
    missing_idempotency_key_handler,
    persistence_error_handler,
    rate_limit_exceeded_handler,
    validation_exception_handler,
)
from transaction_platform.api.schemas import CreateTransactionRequest
from transaction_platform.application.authorize_transaction import (
    AuthorizeTransactionCommand,
    AuthorizeTransactionService,
    DownstreamRateLimitExceededError,
    DownstreamRejectedError,
)
from transaction_platform.application.idempotent_orchestrator import (
    IdempotentTransactionOrchestrator,
)
from transaction_platform.common.correlation import (
    CORRELATION_ID_REGEX,
    MissingCorrelationIdError,
    require_correlation_id,
)
from transaction_platform.common.idempotency import (
    MissingIdempotencyKeyError,
    require_idempotency_key,
)
from transaction_platform.infrastructure.database import (
    DatabaseSettings,
    create_engine_and_session_factory,
)
from transaction_platform.infrastructure.idempotency_coordinator import (
    SqlAlchemyIdempotencyCoordinator,
)
from transaction_platform.ports.idempotency_coordinator import (
    IdempotencyCoordinatorPort,
    PersistenceError,
)
from transaction_platform.ports.payment_network import PaymentNetworkPort


def get_payment_network() -> PaymentNetworkPort:
    """Dependency provider placeholder for PaymentNetworkPort."""
    raise RuntimeError("PaymentNetworkPort dependency is not configured.")


def get_coordinator() -> IdempotencyCoordinatorPort:
    """Dependency provider placeholder for IdempotencyCoordinatorPort."""
    raise RuntimeError("IdempotencyCoordinatorPort dependency is not configured.")


def get_service(
    payment_network: Annotated[PaymentNetworkPort, Depends(get_payment_network)],
) -> AuthorizeTransactionService:
    """Dependency provider returning AuthorizeTransactionService."""
    return AuthorizeTransactionService(payment_network=payment_network)


def get_orchestrator(
    coordinator: Annotated[IdempotencyCoordinatorPort, Depends(get_coordinator)],
    service: Annotated[AuthorizeTransactionService, Depends(get_service)],
) -> IdempotentTransactionOrchestrator:
    """Dependency provider returning IdempotentTransactionOrchestrator."""
    return IdempotentTransactionOrchestrator(coordinator=coordinator, service=service)


def create_app(
    payment_network: PaymentNetworkPort | None = None,
    coordinator: IdempotencyCoordinatorPort | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    engine: AsyncEngine | None = None,
    http_client: httpx.AsyncClient | None = None,
    emulator_base_url: str = "http://localhost:8001",
    network_timeout_seconds: float = 5.0,
    db_settings: DatabaseSettings | None = None,
) -> FastAPI:
    """Create and configure the main transaction FastAPI application."""
    internal_client: httpx.AsyncClient | None = None
    internal_engine: AsyncEngine | None = None

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

    if coordinator is not None:
        resolved_coordinator = coordinator
    elif session_factory is not None:
        resolved_coordinator = SqlAlchemyIdempotencyCoordinator(session_factory=session_factory)
    else:
        cfg = db_settings or DatabaseSettings()
        internal_engine, resolved_session_factory = create_engine_and_session_factory(cfg)
        resolved_coordinator = SqlAlchemyIdempotencyCoordinator(
            session_factory=resolved_session_factory
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if internal_client is not None:
            await internal_client.aclose()
        if internal_engine is not None:
            await internal_engine.dispose()
        elif engine is not None:
            await engine.dispose()

    app = FastAPI(
        title="Transaction Reliability Platform API",
        description="Main transaction orchestration API service with durable idempotency.",
        lifespan=lifespan,
    )

    app.dependency_overrides[get_payment_network] = lambda: resolved_network
    app.dependency_overrides[get_coordinator] = lambda: resolved_coordinator

    # Register RFC 7807 problem exception handlers
    app.add_exception_handler(MissingCorrelationIdError, missing_correlation_id_handler)
    app.add_exception_handler(MissingIdempotencyKeyError, missing_idempotency_key_handler)
    app.add_exception_handler(PersistenceError, persistence_error_handler)
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
            400: {"description": "Malformed request or missing required headers"},
            409: {"description": "Idempotency conflict, in-progress, or transaction ID conflict"},
            422: {"description": "Validation error"},
            500: {"description": "Persistence or concurrency error"},
            502: {"description": "Downstream payment network rejected request"},
            503: {"description": "Downstream payment network rate limited"},
        },
    )
    async def create_transaction(
        request: CreateTransactionRequest,
        correlation_id: Annotated[str, Depends(require_correlation_id)],
        idempotency_key: Annotated[str, Depends(require_idempotency_key)],
        orchestrator: Annotated[IdempotentTransactionOrchestrator, Depends(get_orchestrator)],
    ) -> Response:
        """Process inbound payment transaction authorization with durable idempotency."""
        command = AuthorizeTransactionCommand(
            transaction_id=request.transaction_id,
            merchant_id=request.merchant_id,
            payment_token=request.payment_token,
            amount=request.amount,
            currency=request.currency,
            correlation_id=correlation_id,
        )

        envelope = await orchestrator.handle_authorization(
            command=command,
            idempotency_key=idempotency_key,
        )

        response_headers = dict(envelope.headers)
        if envelope.is_replay:
            response_headers["Idempotent-Replay"] = "true"

        return JSONResponse(
            status_code=envelope.status_code,
            content=envelope.body,
            headers=response_headers,
            media_type=envelope.media_type,
        )

    return app


app = create_app()
