"""Connexion Google / GitHub : identités OAuth, codes de connexion à usage unique, mot de passe facultatif

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20 16:35:20.760798
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "login_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_login_codes_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_login_codes")),
    )
    op.create_index(op.f("ix_login_codes_code_hash"), "login_codes", ["code_hash"], unique=True)

    op.create_table(
        "oauth_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_oauth_accounts_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_accounts")),
        sa.UniqueConstraint("provider", "subject", name=op.f("uq_oauth_accounts_provider_subject")),
    )
    op.create_index(op.f("ix_oauth_accounts_user_id"), "oauth_accounts", ["user_id"], unique=False)

    # Mode batch : SQLite ne sait pas modifier une colonne, la table est recréée (clés étrangères
    # désactivées par env.py pendant la migration, donc sans cascade sur les tables filles)
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("password_hash", existing_type=sa.String(length=255), nullable=True)


def downgrade() -> None:
    # Les comptes sans mot de passe (Google / GitHub) reçoivent un condensé vide : la colonne
    # redevient obligatoire, et verify_password refuse toujours ce condensé
    op.execute("UPDATE users SET password_hash = '' WHERE password_hash IS NULL")
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("password_hash", existing_type=sa.String(length=255), nullable=False)

    op.drop_table("oauth_accounts")
    op.drop_table("login_codes")
