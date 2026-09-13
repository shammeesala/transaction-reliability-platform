"""SQLAlchemy table definitions, check constraints, and metadata."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

transactions_table = Table(
    "transactions",
    metadata,
    Column("transaction_id", String(128), primary_key=True),
    Column("merchant_id", String(128), nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("status", String(32), nullable=False),
    Column("pending_operation", String(32), nullable=True),
    Column("version", Integer, nullable=False, server_default="0"),
    Column("network_reference", String(128), nullable=True),
    Column("authorization_code", String(128), nullable=True),
    Column("decline_code", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "length(btrim(transaction_id)) BETWEEN 1 AND 128",
        name="chk_transactions_transaction_id_trimmed_length",
    ),
    CheckConstraint(
        "length(btrim(merchant_id)) BETWEEN 1 AND 128",
        name="chk_transactions_merchant_id_trimmed_length",
    ),
    CheckConstraint(
        "amount > 0",
        name="chk_transactions_amount_positive",
    ),
    CheckConstraint(
        "currency ~ '^[A-Z]{3}$'",
        name="chk_transactions_currency_format",
    ),
    CheckConstraint(
        "version >= 0",
        name="chk_transactions_version_non_negative",
    ),
    CheckConstraint(
        "status IN ('CREATED', 'AUTHORIZED', 'DECLINED', 'CAPTURED', "
        "'REFUNDED', 'PENDING_RECONCILIATION', 'FAILED')",
        name="chk_transactions_status_valid",
    ),
    CheckConstraint(
        "pending_operation IS NULL OR pending_operation IN ('AUTHORIZATION', 'CAPTURE', 'REFUND')",
        name="chk_transactions_pending_operation_valid",
    ),
    CheckConstraint(
        "((status = 'PENDING_RECONCILIATION' AND pending_operation IS NOT NULL) OR "
        "(status != 'PENDING_RECONCILIATION' AND pending_operation IS NULL))",
        name="chk_transactions_reconciliation_invariant",
    ),
)

Index("idx_transactions_merchant_id", transactions_table.c.merchant_id)
Index("idx_transactions_status", transactions_table.c.status)

idempotency_records_table = Table(
    "idempotency_records",
    metadata,
    Column("idempotency_key_hash", String(64), primary_key=True),
    Column("request_fingerprint", String(64), nullable=False),
    Column("processing_status", String(32), nullable=False),
    Column(
        "transaction_id",
        String(128),
        ForeignKey("transactions.transaction_id"),
        nullable=False,
    ),
    Column("stored_status_code", Integer, nullable=True),
    Column("stored_response_body", JSONB(none_as_null=True), nullable=True),
    Column("stored_headers", JSONB(none_as_null=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "idempotency_key_hash ~ '^[0-9a-f]{64}$'",
        name="chk_idempotency_key_hash_format",
    ),
    CheckConstraint(
        "request_fingerprint ~ '^[0-9a-f]{64}$'",
        name="chk_idempotency_fingerprint_format",
    ),
    CheckConstraint(
        "processing_status IN ('IN_PROGRESS', 'COMPLETED')",
        name="chk_idempotency_status_valid",
    ),
    CheckConstraint(
        "((processing_status = 'COMPLETED' AND stored_status_code IS NOT NULL "
        "AND stored_response_body IS NOT NULL) OR "
        "(processing_status = 'IN_PROGRESS' AND stored_status_code IS NULL "
        "AND stored_response_body IS NULL AND stored_headers IS NULL))",
        name="chk_idempotency_payload_integrity",
    ),
    CheckConstraint(
        "stored_status_code IS NULL OR (stored_status_code BETWEEN 100 AND 599)",
        name="chk_idempotency_stored_status_code_range",
    ),
    CheckConstraint(
        "stored_headers IS NULL OR ("
        "jsonb_typeof(stored_headers) = 'object' AND "
        "(stored_headers - 'Retry-After') = '{}'::jsonb)",
        name="chk_idempotency_stored_headers_allowlist",
    ),
)

Index("idx_idempotency_records_transaction_id", idempotency_records_table.c.transaction_id)
