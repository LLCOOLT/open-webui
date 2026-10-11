"""add lorebook table

Revision ID: 7e3a9c2b51d4
Revises: d4c1a8e37b62
Create Date: 2026-10-10 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '7e3a9c2b51d4'
down_revision: str | None = 'd4c1a8e37b62'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if 'lorebook' not in inspector.get_table_names():
        op.create_table(
            'lorebook',
            sa.Column('id', sa.Text(), nullable=False),
            sa.Column('user_id', sa.Text(), nullable=False),
            sa.Column('name', sa.Text(), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('data', sa.JSON(), nullable=True),
            sa.Column('meta', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.BigInteger(), nullable=False),
            sa.Column('updated_at', sa.BigInteger(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_lorebook_user', 'lorebook', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_lorebook_user', table_name='lorebook')
    op.drop_table('lorebook')
