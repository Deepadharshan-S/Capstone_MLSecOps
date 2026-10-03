"""merge training_jobs branches

Revision ID: 36501f3ce5e6
Revises: bee587bde3b6, 8a1b2c3d4e5f
Create Date: 2026-10-03 15:35:11.431643

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '36501f3ce5e6'
down_revision: Union[str, Sequence[str], None] = ('bee587bde3b6', '8a1b2c3d4e5f')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
