"""Schéma initial : utilisateurs, tableaux, membres, colonnes, cartes, activité

Revision ID: 0001
Revises:
Create Date: 2026-10-20 13:40:18.055433
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("color", sa.String(length=9), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    op.create_table(
        "boards",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], name=op.f("fk_boards_owner_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_boards")),
    )

    op.create_table(
        "activities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("board_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("message", sa.String(length=300), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["board_id"], ["boards.id"], name=op.f("fk_activities_board_id_boards"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_activities_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_activities")),
    )
    op.create_index(op.f("ix_activities_board_id"), "activities", ["board_id"], unique=False)
    op.create_index(op.f("ix_activities_created_at"), "activities", ["created_at"], unique=False)

    op.create_table(
        "columns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("board_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("position", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["board_id"], ["boards.id"], name=op.f("fk_columns_board_id_boards"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_columns")),
    )
    op.create_index(op.f("ix_columns_board_id"), "columns", ["board_id"], unique=False)

    op.create_table(
        "memberships",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("board_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=10), nullable=False),
        sa.ForeignKeyConstraint(
            ["board_id"], ["boards.id"], name=op.f("fk_memberships_board_id_boards"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_memberships_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memberships")),
        sa.UniqueConstraint("board_id", "user_id", name=op.f("uq_memberships_board_id_user_id")),
    )
    op.create_index(op.f("ix_memberships_board_id"), "memberships", ["board_id"], unique=False)
    op.create_index(op.f("ix_memberships_user_id"), "memberships", ["user_id"], unique=False)

    op.create_table(
        "cards",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("board_id", sa.Integer(), nullable=False),
        sa.Column("column_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("position", sa.Float(), nullable=False),
        sa.Column("assignee_id", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["assignee_id"], ["users.id"], name=op.f("fk_cards_assignee_id_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["board_id"], ["boards.id"], name=op.f("fk_cards_board_id_boards"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["column_id"], ["columns.id"], name=op.f("fk_cards_column_id_columns"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], name=op.f("fk_cards_created_by_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cards")),
    )
    op.create_index(op.f("ix_cards_board_id"), "cards", ["board_id"], unique=False)
    op.create_index(op.f("ix_cards_column_id"), "cards", ["column_id"], unique=False)


def downgrade() -> None:
    # Ordre inverse des dépendances ; les index disparaissent avec leur table
    op.drop_table("cards")
    op.drop_table("memberships")
    op.drop_table("columns")
    op.drop_table("activities")
    op.drop_table("boards")
    op.drop_table("users")
