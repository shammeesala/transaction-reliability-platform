"""FastAPI application factory and routes for the payment network emulator."""

from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import Response

from transaction_platform.emulator.errors import (
    CORRELATION_ID_REGEX,
    MissingCorrelationIdError,
    RateLimitedError,
    UnsupportedTokenError,
    missing_correlation_id_handler,
    rate_limited_handler,
    unsupported_token_handler,
    validation_exception_handler,
)
from transaction_platform.emulator.scenarios import (
    AsyncSleeper,
    execute_authorization_scenario,
    get_async_sleeper,
)
from transaction_platform.emulator.schemas import (
    AuthorizationRequest,
    AuthorizedResponse,
    DeclinedResponse,
)
from transaction_platform.emulator.settings import EmulatorSettings


def get_settings() -> EmulatorSettings:
    """Dependency provider returning default emulator settings."""
    return EmulatorSettings()


def require_correlation_id(
    x_correlation_id: Annotated[str | None, Header(alias="X-Correlation-ID")] = None,
) -> str:
    """Validate presence and format of required X-Correlation-ID header.

    Whitespace is strictly rejected to prevent ambiguous header reflection.
    """
    if x_correlation_id is None or not CORRELATION_ID_REGEX.match(x_correlation_id):
        raise MissingCorrelationIdError()
    return x_correlation_id


def create_app(settings: EmulatorSettings | None = None) -> FastAPI:
    """Create and configure an isolated FastAPI application instance."""
    app = FastAPI(
        title="Payment Network Emulator",
        description="Deterministic test simulator for payment network authorizations.",
    )

    resolved_settings = settings or EmulatorSettings()
    app.dependency_overrides[get_settings] = lambda: resolved_settings

    # Register RFC 7807 problem exception handlers
    app.add_exception_handler(MissingCorrelationIdError, missing_correlation_id_handler)
    app.add_exception_handler(UnsupportedTokenError, unsupported_token_handler)
    app.add_exception_handler(RateLimitedError, rate_limited_handler)
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

    @app.post("/v1/authorizations", response_model=None)
    async def authorize(
        request: AuthorizationRequest,
        correlation_id: Annotated[str, Depends(require_correlation_id)],
        app_settings: Annotated[EmulatorSettings, Depends(get_settings)],
        sleeper: Annotated[AsyncSleeper, Depends(get_async_sleeper)],
    ) -> Response | AuthorizedResponse | DeclinedResponse:
        """Process simulated payment network authorization."""
        return await execute_authorization_scenario(
            request=request,
            timeout_delay=app_settings.timeout_delay_seconds,
            sleeper=sleeper,
        )

    return app


app = create_app()
