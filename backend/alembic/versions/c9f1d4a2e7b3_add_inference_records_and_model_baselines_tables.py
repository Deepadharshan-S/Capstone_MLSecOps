"""add inference_records and model_baselines tables

Revision ID: c9f1d4a2e7b3
Revises: 245e6659b178
Create Date: 2026-10-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c9f1d4a2e7b3'
down_revision: Union[str, Sequence[str], None] = '245e6659b178'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Per-request inference telemetry — the substrate every label-free drift
    # detector measures against.
    op.create_table(
        'inference_records',
        sa.Column('model_name', sa.String(length=100), nullable=False),
        sa.Column('model_version', sa.String(length=50), nullable=True),
        sa.Column('deployment_id', sa.String(length=100), nullable=True),
        sa.Column('rayservice_name', sa.String(length=100), nullable=True),
        sa.Column('request_id', sa.String(length=64), nullable=True),
        sa.Column('client_ip', sa.String(length=64), nullable=True),
        sa.Column('prediction', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('probabilities', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('has_probabilities', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('latency_ms', sa.Float(), nullable=True),
        sa.Column('features', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('features_truncated', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('feature_count', sa.Integer(), nullable=True),
        sa.Column('recorded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_inference_records_model_name'), 'inference_records', ['model_name'], unique=False)
    op.create_index(op.f('ix_inference_records_model_version'), 'inference_records', ['model_version'], unique=False)
    op.create_index(op.f('ix_inference_records_deployment_id'), 'inference_records', ['deployment_id'], unique=False)
    op.create_index(op.f('ix_inference_records_request_id'), 'inference_records', ['request_id'], unique=False)
    op.create_index(op.f('ix_inference_records_recorded_at'), 'inference_records', ['recorded_at'], unique=False)
    op.create_index(op.f('ix_inference_records_created_at'), 'inference_records', ['created_at'], unique=False)
    op.create_index(
        'ix_inference_records_model_version_time',
        'inference_records',
        ['model_name', 'model_version', 'recorded_at'],
        unique=False,
    )

    # The reference state a drift detector compares against, bound to one
    # model version so a deployment change is never mistaken for a data change.
    op.create_table(
        'model_baselines',
        sa.Column('model_name', sa.String(length=100), nullable=False),
        sa.Column('model_version', sa.String(length=50), nullable=False),
        sa.Column('dataset_id', sa.String(length=255), nullable=True),
        sa.Column('dataset_ref', sa.String(length=255), nullable=True),
        sa.Column('feature_stats', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('prediction_dist', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('confidence_stats', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('reference_metrics', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('sample_count', sa.Integer(), nullable=True),
        sa.Column('schema_hash', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
        sa.Column('created_by', sa.String(length=100), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_model_baselines_model_name'), 'model_baselines', ['model_name'], unique=False)
    op.create_index(op.f('ix_model_baselines_model_version'), 'model_baselines', ['model_version'], unique=False)
    op.create_index(op.f('ix_model_baselines_schema_hash'), 'model_baselines', ['schema_hash'], unique=False)
    op.create_index(op.f('ix_model_baselines_status'), 'model_baselines', ['status'], unique=False)
    op.create_index(op.f('ix_model_baselines_created_at'), 'model_baselines', ['created_at'], unique=False)
    op.create_index('ix_model_baselines_name_version', 'model_baselines', ['model_name', 'model_version'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_model_baselines_name_version', table_name='model_baselines')
    op.drop_index(op.f('ix_model_baselines_created_at'), table_name='model_baselines')
    op.drop_index(op.f('ix_model_baselines_status'), table_name='model_baselines')
    op.drop_index(op.f('ix_model_baselines_schema_hash'), table_name='model_baselines')
    op.drop_index(op.f('ix_model_baselines_model_version'), table_name='model_baselines')
    op.drop_index(op.f('ix_model_baselines_model_name'), table_name='model_baselines')
    op.drop_table('model_baselines')

    op.drop_index('ix_inference_records_model_version_time', table_name='inference_records')
    op.drop_index(op.f('ix_inference_records_created_at'), table_name='inference_records')
    op.drop_index(op.f('ix_inference_records_recorded_at'), table_name='inference_records')
    op.drop_index(op.f('ix_inference_records_request_id'), table_name='inference_records')
    op.drop_index(op.f('ix_inference_records_deployment_id'), table_name='inference_records')
    op.drop_index(op.f('ix_inference_records_model_version'), table_name='inference_records')
    op.drop_index(op.f('ix_inference_records_model_name'), table_name='inference_records')
    op.drop_table('inference_records')
