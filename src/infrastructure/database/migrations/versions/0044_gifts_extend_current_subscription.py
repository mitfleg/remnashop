"""Allow gifts to extend existing access with durable expiry checkpoints."""

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Resolve names because 0043 also runs under test schemas with naming conventions.
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_unique_constraints("gifts"):
        if constraint["column_names"] in (["remna_uuid"], ["subscription_id"]):
            op.drop_constraint(constraint["name"], "gifts", type_="unique")
    op.create_index("ix_gifts_remna_uuid", "gifts", ["remna_uuid"])
    op.create_index("ix_gifts_subscription_id", "gifts", ["subscription_id"])
    op.add_column("gifts", sa.Column("renewal_base_expire_at", sa.DateTime(timezone=True)))
    op.add_column("gifts", sa.Column("refund_expire_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    # Refuse rollback if multiple gifts already refer to the same subscription.
    op.create_unique_constraint("gifts_remna_uuid_key", "gifts", ["remna_uuid"])
    op.create_unique_constraint("gifts_subscription_id_key", "gifts", ["subscription_id"])
    op.drop_column("gifts", "refund_expire_at")
    op.drop_column("gifts", "renewal_base_expire_at")
    op.drop_index("ix_gifts_subscription_id", table_name="gifts")
    op.drop_index("ix_gifts_remna_uuid", table_name="gifts")
