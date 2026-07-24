"""initial tenant-aware schema"""

from __future__ import annotations

from alembic import op

from database import Base

# revision identifiers, used by Alembic.
revision = "0001_initial_tenant_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
