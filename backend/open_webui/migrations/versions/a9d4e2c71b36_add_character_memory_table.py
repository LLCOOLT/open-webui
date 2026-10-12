"""add character memory table

Revision ID: a9d4e2c71b36
Revises: c3e7a1f50d92
Create Date: 2026-10-12 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a9d4e2c71b36'
down_revision: str | None = 'c3e7a1f50d92'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if 'character_memory' not in inspector.get_table_names():
        op.create_table(
            'character_memory',
            sa.Column('id', sa.Text(), nullable=False),
            sa.Column('user_id', sa.Text(), nullable=False),
            sa.Column('model_id', sa.Text(), nullable=False),
            sa.Column('data', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.BigInteger(), nullable=False),
            sa.Column('updated_at', sa.BigInteger(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('user_id', 'model_id', name='uq_character_memory_user_model'),
        )
        op.create_index('ix_character_memory_model', 'character_memory', ['model_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_character_memory_model', table_name='character_memory')
    op.drop_table('character_memory')
