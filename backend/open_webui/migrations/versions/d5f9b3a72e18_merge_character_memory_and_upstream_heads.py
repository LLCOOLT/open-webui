"""Merge the character memory migration with upstream migrations

Revision ID: d5f9b3a72e18
Revises: a9d4e2c71b36, 9f6c2a8d1b43
Create Date: 2026-11-21 00:00:00.000000

"""

from typing import Sequence, Union

revision: str = 'd5f9b3a72e18'
down_revision: Union[str, Sequence[str], None] = ('a9d4e2c71b36', '9f6c2a8d1b43')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
