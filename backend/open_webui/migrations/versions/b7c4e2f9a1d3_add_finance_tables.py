"""add finance tables

Revision ID: b7c4e2f9a1d3
Revises: d4c1a8e37b62
Create Date: 2026-10-12
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b7c4e2f9a1d3'
down_revision: Union[str, None] = 'd4c1a8e37b62'
branch_labels = None
depends_on = None


def _index_exists(inspector, index_name, table_name):
    indexes = inspector.get_indexes(table_name)
    return any(idx['name'] == index_name for idx in indexes)


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if 'finance_connection' not in tables:
        op.create_table(
            'finance_connection',
            sa.Column('id', sa.Text(), primary_key=True),
            sa.Column('user_id', sa.Text(), nullable=False),
            sa.Column('name', sa.Text(), nullable=False),
            sa.Column('provider', sa.Text(), nullable=False),
            sa.Column('kind', sa.Text(), nullable=False),
            sa.Column('asset_classes', sa.JSON(), nullable=False),
            sa.Column('is_paper', sa.Boolean(), nullable=False, default=True),
            sa.Column('is_default', sa.Boolean(), nullable=False, default=False),
            sa.Column('credentials', sa.JSON(), nullable=True),
            sa.Column('meta', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.BigInteger(), nullable=False),
            sa.Column('updated_at', sa.BigInteger(), nullable=False),
        )

    if 'finance_bar' not in tables:
        op.create_table(
            'finance_bar',
            sa.Column('provider', sa.Text(), primary_key=True),
            sa.Column('symbol', sa.Text(), primary_key=True),
            sa.Column('timeframe', sa.Text(), primary_key=True),
            sa.Column('ts', sa.BigInteger(), primary_key=True),
            sa.Column('open', sa.Float(), nullable=False),
            sa.Column('high', sa.Float(), nullable=False),
            sa.Column('low', sa.Float(), nullable=False),
            sa.Column('close', sa.Float(), nullable=False),
            sa.Column('volume', sa.Float(), nullable=True),
            sa.Column('vwap', sa.Float(), nullable=True),
        )

    inspector.clear_cache()
    if 'finance_connection' in inspector.get_table_names():
        if not _index_exists(inspector, 'ix_finance_connection_user', 'finance_connection'):
            op.create_index('ix_finance_connection_user', 'finance_connection', ['user_id'])


def downgrade():
    op.drop_index('ix_finance_connection_user', table_name='finance_connection')
    op.drop_table('finance_bar')
    op.drop_table('finance_connection')
