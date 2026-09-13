"""Idempotency header validation, key hashing, and request fingerprinting."""

import hashlib
import json
import re
from typing import Annotated

from fastapi import Header

# Printable ASCII characters (alphanumeric, dot, underscore, colon, hyphen) up to 128 characters
IDEMPOTENCY_KEY_REGEX = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class MissingIdempotencyKeyError(Exception):
    """Raised when Idempotency-Key header is missing, blank, or invalid format."""


def require_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    """Validate presence and format of required Idempotency-Key header.

    Whitespace is strictly rejected to prevent ambiguous header reflection.
    """
    if idempotency_key is None or not IDEMPOTENCY_KEY_REGEX.match(idempotency_key):
        raise MissingIdempotencyKeyError()
    return idempotency_key


def compute_key_hash(idempotency_key: str) -> str:
    """Compute deterministic SHA-256 hex digest of the raw idempotency key.

    Note on Security:
    The raw idempotency key is not persisted. SHA-256 provides a deterministic
    fixed-width 64-character hexadecimal lookup digest for primary key indexing.
    Predictable or low-entropy keys can still be recovered through offline guessing.
    Clients should use high-entropy random idempotency keys (e.g. UUIDv4).
    """
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()


def compute_request_fingerprint(
    amount: int,
    currency: str,
    merchant_id: str,
    payment_token: str,
    transaction_id: str,
) -> str:
    """Compute deterministic SHA-256 hex digest of canonical logical request payload.

    Excludes transport metadata (X-Correlation-ID and Idempotency-Key).
    """
    payload_dict = {
        "amount": amount,
        "currency": currency,
        "merchant_id": merchant_id,
        "payment_token": payment_token,
        "transaction_id": transaction_id,
    }
    canonical_json = json.dumps(
        payload_dict,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
