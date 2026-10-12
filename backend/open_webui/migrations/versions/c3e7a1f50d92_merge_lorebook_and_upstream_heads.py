"""Merge the lorebook migration with upstream migrations

Revision ID: c3e7a1f50d92
Revises: 7e3a9c2b51d4, b8f2c6d91a04
Create Date: 2026-10-12 00:00:00.000000

"""

from typing import Sequence, Union

revision: str = 'c3e7a1f50d92'
down_revision: Union[str, Sequence[str], None] = ('7e3a9c2b51d4', 'b8f2c6d91a04')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
