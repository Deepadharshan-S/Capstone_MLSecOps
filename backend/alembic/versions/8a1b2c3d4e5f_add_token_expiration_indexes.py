"""add_token_expiration_indexes

Revision ID: 8a1b2c3d4e5f
Revises: 710094a7e4a1
Create Date: 2026-09-23 09:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8a1b2c3d4e5f'
down_revision: Union[str, Sequence[str], None] = '710094a7e4a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: add performance indexes for token expiration cleanup."""
    op.create_index(
        op.f('ix_refresh_tokens_expires_at'),
        'refresh_tokens',
        ['expires_at'],
        unique=False,
    )
    op.create_index(
        op.f('ix_blacklisted_tokens_expires_at'),
        'blacklisted_tokens',
        ['expires_at'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema: drop token expiration indexes."""
    op.drop_index(
        op.f('ix_blacklisted_tokens_expires_at'),
        table_name='blacklisted_tokens',
    )
    op.drop_index(
        op.f('ix_refresh_tokens_expires_at'),
        table_name='refresh_tokens',
    )
