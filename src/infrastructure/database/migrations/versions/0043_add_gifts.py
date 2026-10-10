"""Paid gifts with a fixed recipient and recoverable provisioning."""

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gifts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token", sa.UUID(), nullable=False, unique=True),
        sa.Column("buyer_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("recipient_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("recipient_name", sa.String(), nullable=False),
        sa.Column("recipient_username", sa.String(), nullable=True),
        sa.Column("remna_uuid", sa.UUID(), nullable=False, unique=True),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("subscriptions.id", ondelete="SET NULL"),
            unique=True,
        ),
        *[
            sa.Column(name, sa.DateTime(timezone=True), nullable=True)
            for name in (
                "paid_at",
                "expire_at",
                "issued_at",
                "buyer_notified_at",
                "buyer_unreachable_at",
                "recipient_notified_at",
                "recipient_unreachable_at",
                "recipient_seen_at",
            )
        ],
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_gifts_buyer_id", "gifts", ["buyer_id"])
    op.create_index("ix_gifts_recipient_telegram_id", "gifts", ["recipient_telegram_id"])
    op.add_column(
        "transactions",
        sa.Column(
            "gift_id",
            sa.Integer(),
            sa.ForeignKey("gifts.id"),
            nullable=True,
        ),
    )
    op.create_unique_constraint("uq_transactions_gift_id", "transactions", ["gift_id"])
    op.add_column("transactions", sa.Column("confirmation_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("transactions", "confirmation_url")
    op.drop_constraint("uq_transactions_gift_id", "transactions", type_="unique")
    op.drop_column("transactions", "gift_id")
    op.drop_table("gifts")
