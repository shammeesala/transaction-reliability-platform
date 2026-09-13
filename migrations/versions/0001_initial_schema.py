"""Initial schema for transactions and idempotency records.

Revision ID: 0001
Revises:
Create Date: 2026-09-13 16:40:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transactions",
        sa.Column("transaction_id", sa.String(length=128), nullable=False),
        sa.Column("merchant_id", sa.String(length=128), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("pending_operation", sa.String(length=32), nullable=True),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("network_reference", sa.String(length=128), nullable=True),
        sa.Column("authorization_code", sa.String(length=128), nullable=True),
        sa.Column("decline_code", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("transaction_id"),
        sa.CheckConstraint(
            "length(btrim(transaction_id)) BETWEEN 1 AND 128",
            name="chk_transactions_transaction_id_trimmed_length",
        ),
        sa.CheckConstraint(
            "length(btrim(merchant_id)) BETWEEN 1 AND 128",
            name="chk_transactions_merchant_id_trimmed_length",
        ),
        sa.CheckConstraint(
            "amount > 0",
            name="chk_transactions_amount_positive",
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="chk_transactions_currency_format",
        ),
        sa.CheckConstraint(
            "version >= 0",
            name="chk_transactions_version_non_negative",
        ),
        sa.CheckConstraint(
            "status IN ('CREATED', 'AUTHORIZED', 'DECLINED', 'CAPTURED', "
            "'REFUNDED', 'PENDING_RECONCILIATION', 'FAILED')",
            name="chk_transactions_status_valid",
        ),
        sa.CheckConstraint(
            "pending_operation IS NULL OR "
            "pending_operation IN ('AUTHORIZATION', 'CAPTURE', 'REFUND')",
            name="chk_transactions_pending_operation_valid",
        ),
        sa.CheckConstraint(
            "((status = 'PENDING_RECONCILIATION' AND pending_operation IS NOT NULL) OR "
            "(status != 'PENDING_RECONCILIATION' AND pending_operation IS NULL))",
            name="chk_transactions_reconciliation_invariant",
        ),
    )
    op.create_index("idx_transactions_merchant_id", "transactions", ["merchant_id"], unique=False)
    op.create_index("idx_transactions_status", "transactions", ["status"], unique=False)

    op.create_table(
        "idempotency_records",
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("processing_status", sa.String(length=32), nullable=False),
        sa.Column("transaction_id", sa.String(length=128), nullable=False),
        sa.Column("stored_status_code", sa.Integer(), nullable=True),
        sa.Column("stored_response_body", JSONB, nullable=True),
        sa.Column("stored_headers", JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.transaction_id"]),
        sa.PrimaryKeyConstraint("idempotency_key_hash"),
        sa.CheckConstraint(
            "idempotency_key_hash ~ '^[0-9a-f]{64}$'",
            name="chk_idempotency_key_hash_format",
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="chk_idempotency_fingerprint_format",
        ),
        sa.CheckConstraint(
            "processing_status IN ('IN_PROGRESS', 'COMPLETED')",
            name="chk_idempotency_status_valid",
        ),
        sa.CheckConstraint(
            "((processing_status = 'COMPLETED' AND stored_status_code IS NOT NULL "
            "AND stored_response_body IS NOT NULL) OR "
            "(processing_status = 'IN_PROGRESS' AND stored_status_code IS NULL "
            "AND stored_response_body IS NULL AND stored_headers IS NULL))",
            name="chk_idempotency_payload_integrity",
        ),
        sa.CheckConstraint(
            "stored_status_code IS NULL OR (stored_status_code BETWEEN 100 AND 599)",
            name="chk_idempotency_stored_status_code_range",
        ),
        sa.CheckConstraint(
            "stored_headers IS NULL OR ("
            "jsonb_typeof(stored_headers) = 'object' AND "
            "(stored_headers - 'Retry-After') = '{}'::jsonb)",
            name="chk_idempotency_stored_headers_allowlist",
        ),
    )
    op.create_index(
        "idx_idempotency_records_transaction_id",
        "idempotency_records",
        ["transaction_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_idempotency_records_transaction_id", table_name="idempotency_records")
    op.drop_table("idempotency_records")
    op.drop_index("idx_transactions_status", table_name="transactions")
    op.drop_index("idx_transactions_merchant_id", table_name="transactions")
    op.drop_table("transactions")
