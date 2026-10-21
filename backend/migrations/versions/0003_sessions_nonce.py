"""Révocation des sessions (users.token_version) et code de connexion lié au navigateur (nonce)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-21 09:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Les comptes existants partent de 0 : les jetons déjà émis (sans `ver`, lu comme 0) restent valides
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))

    # Codes en cours (60 s au plus) : sans nonce, ils ne pourraient plus être échangés
    op.execute("DELETE FROM login_codes")
    with op.batch_alter_table("login_codes") as batch_op:
        batch_op.add_column(sa.Column("nonce_hash", sa.String(length=64), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("login_codes") as batch_op:
        batch_op.drop_column("nonce_hash")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("token_version")
