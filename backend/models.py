from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from database import Base

OWNER = "owner"
MEMBER = "member"

# Couleurs d'avatar attribuées à l'inscription (lisibles en clair comme en sombre)
AVATAR_COLORS = ["#6366f1", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444", "#ec4899", "#8b5cf6", "#14b8a6"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(80), nullable=False)
    password_hash = Column(String(255), nullable=False)
    color = Column(String(9), nullable=False)
    created_at = Column(DateTime, default=_utcnow)


class Board(Base):
    __tablename__ = "boards"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    # Incrémenté à chaque modification : les clients détectent ainsi un événement manqué
    revision = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=_utcnow)

    columns = relationship(
        "BoardColumn", cascade="all, delete-orphan", passive_deletes=True,
        order_by="[BoardColumn.position, BoardColumn.id]",
    )
    memberships = relationship("Membership", cascade="all, delete-orphan", passive_deletes=True)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("board_id", "user_id"),)

    id = Column(Integer, primary_key=True)
    board_id = Column(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String(10), nullable=False, default=MEMBER)

    user = relationship("User")


class BoardColumn(Base):
    __tablename__ = "columns"

    id = Column(Integer, primary_key=True)
    board_id = Column(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(60), nullable=False)
    position = Column(Float, nullable=False)

    # Départage par id : deux déplacements simultanés peuvent produire la même position, l'ordre
    # reste alors déterministe (et identique côté client)
    cards = relationship(
        "Card", cascade="all, delete-orphan", passive_deletes=True, order_by="[Card.position, Card.id]"
    )


class Card(Base):
    __tablename__ = "cards"

    id = Column(Integer, primary_key=True)
    board_id = Column(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False, index=True)
    column_id = Column(Integer, ForeignKey("columns.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=False, default="")
    # Indexation fractionnaire : insérer entre deux cartes = moyenne de leurs positions
    position = Column(Float, nullable=False)
    assignee_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # Verrou optimiste pour les éditions de contenu (titre, description, assignation)
    version = Column(Integer, nullable=False, default=1)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class Activity(Base):
    __tablename__ = "activities"

    id = Column(Integer, primary_key=True)
    board_id = Column(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    message = Column(String(300), nullable=False)
    created_at = Column(DateTime, default=_utcnow, index=True)

    user = relationship("User")
