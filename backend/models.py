from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import relationship

from database import Base

OWNER = "owner"
MEMBER = "member"

# Couleurs d'avatar attribuées à l'inscription (lisibles en clair comme en sombre)
AVATAR_COLORS = ["#6366f1", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444", "#ec4899", "#8b5cf6", "#14b8a6"]


def next_avatar_color(db) -> str:
    """Couleurs attribuées à tour de rôle, selon le nombre de comptes existants."""
    return AVATAR_COLORS[db.query(func.count(User.id)).scalar() % len(AVATAR_COLORS)]


# Dates en UTC sans fuseau (DATETIME en SQLite, TIMESTAMP WITHOUT TIME ZONE en Postgres) : même
# valeur et même format ISO renvoyé quelle que soit la base ou le fuseau de la session Postgres ;
# le client sait qu'elles sont en UTC (format.ts ajoute le « Z »)
def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(80), nullable=False)
    # NULL pour un compte créé via Google ou GitHub (pas de connexion par mot de passe possible)
    password_hash = Column(String(255), nullable=True)
    color = Column(String(9), nullable=False)
    created_at = Column(DateTime, default=_utcnow)
    # Recopié dans chaque jeton de session (`ver`) : l'incrémenter ferme toutes les sessions ouvertes
    token_version = Column(Integer, nullable=False, default=0, server_default="0")


class OAuthAccount(Base):
    """Identité chez un fournisseur (Google, GitHub) rattachée à un compte. Un compte peut en avoir
    plusieurs ; une identité (fournisseur, identifiant chez lui) n'appartient qu'à un compte."""

    __tablename__ = "oauth_accounts"
    __table_args__ = (UniqueConstraint("provider", "subject"),)

    id = Column(Integer, primary_key=True)
    provider = Column(String(20), nullable=False)
    # Identifiant stable chez le fournisseur (`sub` Google, `id` GitHub) : l'e-mail peut changer
    subject = Column(String(255), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, default=_utcnow)

    user = relationship("User")


class LoginCode(Base):
    """Code de connexion à usage unique et de courte durée, remis au frontend dans l'URL de retour
    OAuth à la place du jeton de session. En base (et non en mémoire) : l'échange peut arriver
    sur une autre instance que le retour OAuth. Seul son condensé est stocké.

    Lié au navigateur qui a lancé la connexion par un nonce (gardé en sessionStorage, transmis à
    /start puis exigé à l'échange) : un code obtenu par quelqu'un d'autre ne peut pas être utilisé
    pour connecter un tiers à son compte (« login CSRF »)."""

    __tablename__ = "login_codes"

    id = Column(Integer, primary_key=True)
    code_hash = Column(String(64), nullable=False, unique=True, index=True)
    nonce_hash = Column(String(64), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    expires_at = Column(DateTime, nullable=False)


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
