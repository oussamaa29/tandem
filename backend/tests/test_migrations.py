import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from conftest import drop_schema
from database import Base, alembic_config, engine, normalize_url, upgrade_schema


@pytest.fixture
def empty_db():
    """Base vide (ni tables ni version Alembic), puis schéma restauré pour les tests suivants."""
    drop_schema()
    yield
    drop_schema()
    upgrade_schema()


def _foreign_keys(table_names):
    """{(table, colonnes, table cible, ON DELETE)} tel que la base le rapporte."""
    insp = inspect(engine)
    return {
        (t, tuple(fk["constrained_columns"]), fk["referred_table"], (fk["options"].get("ondelete") or "").upper())
        for t in table_names
        for fk in insp.get_foreign_keys(t)
    }


def test_migrations_create_exactly_the_model_schema(empty_db):
    assert inspect(engine).get_table_names() == []
    command.upgrade(alembic_config(), "head")
    # Alembic migre avec son propre moteur : les connexions SQLite déjà ouvertes gardent l'ancien
    # schéma en cache pour les PRAGMA (utilisés par la réflexion), on repart de connexions neuves
    engine.dispose()

    # Aucune différence entre la base migrée et les modèles : tables, colonnes, types, nullabilité,
    # index, contraintes uniques et clés étrangères (une migration oubliée échouerait ici)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == []

    # Les ON DELETE ne sont pas toujours comparés par Alembic : vérification explicite
    expected = {
        (t.name, tuple(c.name for c in fk.columns), fk.referred_table.name, (fk.ondelete or "").upper())
        for t in Base.metadata.sorted_tables
        for fk in t.foreign_key_constraints
    }
    assert _foreign_keys(Base.metadata.tables) == expected
    assert ("cards", ("assignee_id",), "users", "SET NULL") in expected
    assert ("cards", ("column_id",), "columns", "CASCADE") in expected


def test_downgrade_removes_everything_and_upgrade_is_repeatable(empty_db):
    cfg = alembic_config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    command.upgrade(cfg, "head")
    assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())


def test_database_created_before_alembic_is_stamped_not_recreated(empty_db):
    # Base locale créée par l'ancien create_all : même schéma que la migration initiale, sans
    # table alembic_version. Elle est marquée comme migrée, sans erreur « table already exists ».
    Base.metadata.create_all(bind=engine)
    upgrade_schema()
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    with engine.connect() as conn:
        assert MigrationContext.configure(conn).get_current_revision() == head


def test_postgres_urls_use_psycopg3():
    assert normalize_url("postgresql://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    assert normalize_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("sqlite:///./tandem.db") == "sqlite:///./tandem.db"
