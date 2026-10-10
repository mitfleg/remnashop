from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "broadcast_messages",
        sa.Column("message_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.execute("""
        UPDATE broadcast_messages
        SET message_ids = jsonb_build_array(message_id)
        WHERE message_id IS NOT NULL
    """)


def downgrade() -> None:
    op.drop_column("broadcast_messages", "message_ids")
