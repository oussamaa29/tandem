import os
from pathlib import Path

from sqlalchemy import MetaData, create_engine, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker

from config import settings

BACKEND_DIR = Path(__file__).resolve().parent
# Révision de la migration initiale : schéma identique à l'ancien create_all
INITIAL_REVISION = "0001"


def normalize_url(url: str) -> str:
    """`postgresql://` (et `postgres://`, fourni par certains hébergeurs) désigne le pilote psycopg2
    pour SQLAlchemy : on force psycopg 3, le seul installé."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


DATABASE_URL = normalize_url(settings.DATABASE_URL)
is_sqlite = DATABASE_URL.startswith("sqlite")

if is_sqlite:
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

    # SQLite n'applique les clés étrangères (et donc les ON DELETE CASCADE) que si on le demande
    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
else:
    engine = create_engine(
        DATABASE_URL,
        # Vérifie la connexion avant de la réutiliser : un Postgres hébergé ferme les connexions inactives
        pool_pre_ping=True,
        pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
        max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "10")),
    )


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
# Contraintes nommées de façon déterministe : une migration future peut les supprimer ou les
# modifier par leur nom, identique sur SQLite et Postgres (sinon chaque base invente le sien)
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
Base = declarative_base(metadata=MetaData(naming_convention=NAMING_CONVENTION))


def is_unique_violation(exc: IntegrityError) -> bool:
    """Violation d'unicité (et non de clé étrangère, de NOT NULL…) : code SQLSTATE 23505 en
    Postgres ; SQLite ne fournit qu'un message (« UNIQUE constraint failed: … »)."""
    if getattr(exc.orig, "sqlstate", None) == "23505":
        return True
    return "UNIQUE constraint failed" in str(exc.orig)


def alembic_config():
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    # Appel depuis l'application ou les tests : on ne reconfigure pas leur journalisation
    cfg.attributes["configure_logging"] = False
    return cfg


def _rebuild_legacy_sqlite_tables() -> None:
    """Base SQLite créée avant Alembic par create_all : même schéma que la migration initiale,
    mais contraintes (clés primaires, étrangères, unicité) sans nom. Chaque table est recréée à
    l'identique avec les noms de NAMING_CONVENTION, données copiées, pour que les migrations
    suivantes (et l'autogénération) voient exactement le schéma de la migration initiale.

    Moteur dédié sans PRAGMA foreign_keys=ON : la recréation copie la table puis supprime
    l'ancienne, ce qui déclencherait sinon les ON DELETE CASCADE des tables filles."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.pool import NullPool

    import models  # noqa: F401  (enregistre les tables dans Base.metadata)

    rebuild_engine = create_engine(DATABASE_URL, poolclass=NullPool)
    try:
        with rebuild_engine.begin() as conn:
            op = Operations(MigrationContext.configure(conn))
            for table in Base.metadata.sorted_tables:
                with op.batch_alter_table(table.name, recreate="always", copy_from=table):
                    pass
                # Avec copy_from, Alembic ne recrée pas les index de l'ancienne table
                for index in table.indexes:
                    index.create(conn, checkfirst=True)
    finally:
        rebuild_engine.dispose()
    # Les connexions déjà ouvertes gardent l'ancien schéma en cache pour les PRAGMA
    engine.dispose()


def upgrade_schema() -> None:
    """Équivalent de `alembic upgrade head`. Une base SQLite créée avant Alembic (par create_all,
    sans table alembic_version) est d'abord remise aux noms de contraintes de la migration
    initiale, puis marquée comme étant à cette migration. Pas d'équivalent en Postgres : aucune
    base de production n'a été créée par create_all, et une base inconnue doit faire échouer la
    migration (« table already exists ») plutôt qu'être adoptée en silence."""
    from alembic import command

    cfg = alembic_config()
    tables = inspect(engine).get_table_names()
    if is_sqlite and "alembic_version" not in tables and "users" in tables:
        _rebuild_legacy_sqlite_tables()
        command.stamp(cfg, INITIAL_REVISION)
    command.upgrade(cfg, "head")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
