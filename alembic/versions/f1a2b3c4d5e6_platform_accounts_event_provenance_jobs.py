"""platform accounts, event provenance, job queue

Revision ID: f1a2b3c4d5e6
Revises: d4e5f6a7b8c9
Create Date: 2026-09-01 00:00:00.000000

Phase A / item A1 of FIX.md:
  * seller_platform_accounts — resolves a platform-native seller id to ours,
    and owns the per-platform credentials that no longer fit one Amazon-shaped
    column on `sellers`.
  * events.platform / dedup_key / raw_payload — provenance, plus the unique
    index that makes an at-least-once redelivery a no-op.
  * jobs — the durable queue the worker process claims from.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_accounts = sa.table(
    'seller_platform_accounts',
    sa.column('id', sa.String),
    sa.column('platform', sa.String),
    sa.column('external_id', sa.String),
    sa.column('seller_id', sa.String),
    sa.column('credentials', sa.JSON),
    sa.column('created_at', sa.DateTime(timezone=True)),
)

_sellers = sa.table(
    'sellers',
    sa.column('id', sa.String),
    sa.column('sp_api_credentials', sa.JSON),
)


def upgrade() -> None:
    op.create_table(
        'seller_platform_accounts',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('platform', sa.String(), nullable=False),
        sa.Column('external_id', sa.String(), nullable=False),
        sa.Column('seller_id', sa.String(), nullable=False),
        sa.Column('credentials', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('platform', 'external_id', name='uq_account_platform_external_id'),
    )
    op.create_index(
        'ix_seller_platform_accounts_seller_id',
        'seller_platform_accounts',
        ['seller_id'],
    )

    op.create_table(
        'jobs',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('event_id', sa.String(), nullable=False),
        sa.Column('seller_id', sa.String(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('run_after', sa.DateTime(timezone=True), nullable=False),
        sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('target_quantity', sa.Integer(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_jobs_event_id', 'jobs', ['event_id'])
    op.create_index('ix_jobs_status_run_after', 'jobs', ['status', 'run_after'])

    with op.batch_alter_table('events') as batch_op:
        batch_op.add_column(sa.Column('platform', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('dedup_key', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('raw_payload', sa.JSON(), nullable=True))
        batch_op.create_unique_constraint(
            'uq_event_platform_dedup_key', ['platform', 'dedup_key']
        )

    _backfill_amazon_accounts()

    with op.batch_alter_table('sellers') as batch_op:
        batch_op.drop_column('sp_api_credentials')


def _backfill_amazon_accounts() -> None:
    """Move existing Amazon credentials off `sellers` onto account rows.

    Pre-existing sellers have no merchant token recorded — nothing ever
    captured one — so they get a `legacy:` placeholder external_id. It keeps
    the unique constraint satisfiable and is deliberately unmatchable by a real
    inbound push: those sellers must reconnect through OAuth, which supplies
    the real `selling_partner_id`.
    """
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(_sellers.c.id, _sellers.c.sp_api_credentials)
        .where(_sellers.c.sp_api_credentials.isnot(None))
    ).fetchall()
    if not rows:
        return

    now = datetime.now(timezone.utc)
    bind.execute(
        _accounts.insert(),
        [
            {
                'id': str(uuid.uuid4()),
                'platform': 'amazon',
                'external_id': f'legacy:{seller_id}',
                'seller_id': seller_id,
                'credentials': {**credentials, 'platform': 'amazon'},
                'created_at': now,
            }
            for seller_id, credentials in rows
        ],
    )


def downgrade() -> None:
    with op.batch_alter_table('sellers') as batch_op:
        batch_op.add_column(sa.Column('sp_api_credentials', sa.JSON(), nullable=True))

    bind = op.get_bind()
    rows = bind.execute(
        sa.select(_accounts.c.seller_id, _accounts.c.credentials)
        .where(_accounts.c.platform == 'amazon')
        .where(_accounts.c.credentials.isnot(None))
    ).fetchall()
    for seller_id, credentials in rows:
        bind.execute(
            _sellers.update()
            .where(_sellers.c.id == seller_id)
            .values(sp_api_credentials=credentials)
        )

    with op.batch_alter_table('events') as batch_op:
        batch_op.drop_constraint('uq_event_platform_dedup_key', type_='unique')
        batch_op.drop_column('raw_payload')
        batch_op.drop_column('dedup_key')
        batch_op.drop_column('platform')

    op.drop_index('ix_jobs_status_run_after', table_name='jobs')
    op.drop_index('ix_jobs_event_id', table_name='jobs')
    op.drop_table('jobs')

    op.drop_index('ix_seller_platform_accounts_seller_id', table_name='seller_platform_accounts')
    op.drop_table('seller_platform_accounts')
