"""add finance strategy and backtest tables

Revision ID: c8d5f3a0b2e4
Revises: b7c4e2f9a1d3
Create Date: 2026-10-12
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c8d5f3a0b2e4'
down_revision: Union[str, None] = 'b7c4e2f9a1d3'
branch_labels = None
depends_on = None


def _index_exists(inspector, index_name, table_name):
    indexes = inspector.get_indexes(table_name)
    return any(idx['name'] == index_name for idx in indexes)


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if 'finance_strategy' not in tables:
        op.create_table(
            'finance_strategy',
            sa.Column('id', sa.Text(), primary_key=True),
            sa.Column('user_id', sa.Text(), nullable=False),
            sa.Column('name', sa.Text(), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('kind', sa.Text(), nullable=False),
            sa.Column('definition', sa.JSON(), nullable=True),
            sa.Column('source', sa.Text(), nullable=True),
            sa.Column('params', sa.JSON(), nullable=False),
            sa.Column('timeframe', sa.Text(), nullable=False),
            sa.Column('universe', sa.JSON(), nullable=False),
            sa.Column('engine', sa.Text(), nullable=False, server_default='builtin'),
            sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('meta', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.BigInteger(), nullable=False),
            sa.Column('updated_at', sa.BigInteger(), nullable=False),
        )

    if 'finance_strategy_version' not in tables:
        op.create_table(
            'finance_strategy_version',
            sa.Column('id', sa.Text(), primary_key=True),
            sa.Column('strategy_id', sa.Text(), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('definition', sa.JSON(), nullable=True),
            sa.Column('source', sa.Text(), nullable=True),
            sa.Column('params', sa.JSON(), nullable=False),
            sa.Column('created_at', sa.BigInteger(), nullable=False),
        )

    if 'finance_backtest' not in tables:
        op.create_table(
            'finance_backtest',
            sa.Column('id', sa.Text(), primary_key=True),
            sa.Column('user_id', sa.Text(), nullable=False),
            sa.Column('strategy_id', sa.Text(), nullable=True),
            sa.Column('strategy_version', sa.Integer(), nullable=True),
            sa.Column('name', sa.Text(), nullable=False),
            sa.Column('config', sa.JSON(), nullable=False),
            sa.Column('status', sa.Text(), nullable=False),
            sa.Column('error', sa.Text(), nullable=True),
            sa.Column('metrics', sa.JSON(), nullable=True),
            sa.Column('benchmark', sa.JSON(), nullable=True),
            sa.Column('result', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.BigInteger(), nullable=False),
            sa.Column('updated_at', sa.BigInteger(), nullable=False),
            sa.Column('completed_at', sa.BigInteger(), nullable=True),
        )

    inspector.clear_cache()
    indexes = [
        ('ix_finance_strategy_user', 'finance_strategy', ['user_id']),
        ('ix_finance_strategy_version_sid', 'finance_strategy_version', ['strategy_id', 'version']),
        ('ix_finance_backtest_user_created', 'finance_backtest', ['user_id', 'created_at']),
        ('ix_finance_backtest_strategy', 'finance_backtest', ['strategy_id']),
    ]
    existing = inspector.get_table_names()
    for name, table, cols in indexes:
        if table in existing and not _index_exists(inspector, name, table):
            op.create_index(name, table, cols)


def downgrade():
    op.drop_index('ix_finance_backtest_strategy', table_name='finance_backtest')
    op.drop_index('ix_finance_backtest_user_created', table_name='finance_backtest')
    op.drop_index('ix_finance_strategy_version_sid', table_name='finance_strategy_version')
    op.drop_index('ix_finance_strategy_user', table_name='finance_strategy')
    op.drop_table('finance_backtest')
    op.drop_table('finance_strategy_version')
    op.drop_table('finance_strategy')
