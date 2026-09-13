"""Unit tests for idempotency primitives: header validation, key hashing, and fingerprinting."""

import pytest

from transaction_platform.common.idempotency import (
    MissingIdempotencyKeyError,
    compute_key_hash,
    compute_request_fingerprint,
    require_idempotency_key,
)


class TestIdempotencyKeyValidation:
    """Verify Idempotency-Key header syntax, length, and whitespace rejection."""

    def test_valid_keys_accepted(self) -> None:
        valid_keys = [
            "idem_12345",
            "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
            "order.123:req_abc-XYZ",
            "a" * 128,
        ]
        for key in valid_keys:
            assert require_idempotency_key(key) == key

    def test_missing_or_none_rejected(self) -> None:
        with pytest.raises(MissingIdempotencyKeyError):
            require_idempotency_key(None)

    def test_empty_or_whitespace_rejected(self) -> None:
        invalid_keys = ["", "   ", "\t", "\n", "key with spaces", " key", "key "]
        for key in invalid_keys:
            with pytest.raises(MissingIdempotencyKeyError):
                require_idempotency_key(key)

    def test_invalid_characters_rejected(self) -> None:
        invalid_keys = ["key#1", "key/2", "key?3", "key!4", "key@5", "key$6"]
        for key in invalid_keys:
            with pytest.raises(MissingIdempotencyKeyError):
                require_idempotency_key(key)

    def test_key_exceeding_128_chars_rejected(self) -> None:
        too_long = "a" * 129
        with pytest.raises(MissingIdempotencyKeyError):
            require_idempotency_key(too_long)


class TestKeyHashing:
    """Verify SHA-256 key hashing determinism and format."""

    def test_sha256_hash_format(self) -> None:
        digest = compute_key_hash("test_key_123")
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_key_hash_determinism(self) -> None:
        key = "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
        h1 = compute_key_hash(key)
        h2 = compute_key_hash(key)
        assert h1 == h2

    def test_different_keys_produce_different_hashes(self) -> None:
        h1 = compute_key_hash("key_1")
        h2 = compute_key_hash("key_2")
        assert h1 != h2


class TestRequestFingerprinting:
    """Verify canonical payload fingerprinting determinism and exclusion of transport metadata."""

    def test_fingerprint_determinism(self) -> None:
        f1 = compute_request_fingerprint(
            amount=5000,
            currency="USD",
            merchant_id="merchant_1",
            payment_token="tok_test_approved",
            transaction_id="txn_100",
        )
        f2 = compute_request_fingerprint(
            amount=5000,
            currency="USD",
            merchant_id="merchant_1",
            payment_token="tok_test_approved",
            transaction_id="txn_100",
        )
        assert f1 == f2
        assert len(f1) == 64
        assert all(c in "0123456789abcdef" for c in f1)

    def test_any_payload_mutation_changes_fingerprint(self) -> None:
        base_kwargs = {
            "amount": 5000,
            "currency": "USD",
            "merchant_id": "merchant_1",
            "payment_token": "tok_test_approved",
            "transaction_id": "txn_100",
        }
        base_fp = compute_request_fingerprint(**base_kwargs)  # type: ignore[arg-type]

        # Mutate amount
        assert compute_request_fingerprint(
            **{**base_kwargs, "amount": 5001}  # type: ignore[arg-type]
        ) != base_fp

        # Mutate currency
        assert compute_request_fingerprint(
            **{**base_kwargs, "currency": "EUR"}  # type: ignore[arg-type]
        ) != base_fp

        # Mutate merchant_id
        assert compute_request_fingerprint(
            **{**base_kwargs, "merchant_id": "merchant_2"}  # type: ignore[arg-type]
        ) != base_fp

        # Mutate payment_token
        assert compute_request_fingerprint(
            **{**base_kwargs, "payment_token": "tok_test_declined"}  # type: ignore[arg-type]
        ) != base_fp

        # Mutate transaction_id
        assert compute_request_fingerprint(
            **{**base_kwargs, "transaction_id": "txn_101"}  # type: ignore[arg-type]
        ) != base_fp
