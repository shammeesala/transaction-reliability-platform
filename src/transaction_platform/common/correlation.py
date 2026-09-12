"""Shared correlation-ID validation and header utilities."""

import re
from typing import Annotated

from fastapi import Header

CORRELATION_ID_REGEX = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class MissingCorrelationIdError(Exception):
    """Raised when X-Correlation-ID header is missing, blank, or invalid."""


def require_correlation_id(
    x_correlation_id: Annotated[str | None, Header(alias="X-Correlation-ID")] = None,
) -> str:
    """Validate presence and format of required X-Correlation-ID header.

    Whitespace is strictly rejected to prevent ambiguous header reflection.
    """
    if x_correlation_id is None or not CORRELATION_ID_REGEX.match(x_correlation_id):
        raise MissingCorrelationIdError()
    return x_correlation_id
