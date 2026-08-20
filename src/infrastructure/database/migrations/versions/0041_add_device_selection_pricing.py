from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "plans",
        sa.Column("max_device_limit", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute("UPDATE plans SET max_device_limit = device_limit")
    op.add_column(
        "plan_prices",
        sa.Column(
            "extra_device_price",
            sa.Numeric(precision=12, scale=4),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_plans_max_device_limit",
        "plans",
        "max_device_limit >= device_limit",
    )
    op.create_check_constraint(
        "ck_plan_prices_extra_device_price",
        "plan_prices",
        "extra_device_price >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_plan_prices_extra_device_price", "plan_prices", type_="check")
    op.drop_constraint("ck_plans_max_device_limit", "plans", type_="check")
    op.drop_column("plan_prices", "extra_device_price")
    op.drop_column("plans", "max_device_limit")
