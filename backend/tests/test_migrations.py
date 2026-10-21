import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from conftest import drop_schema
from database import Base, alembic_config, engine, is_sqlite, normalize_url, upgrade_schema


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


def test_downgrade_of_0002_keeps_passwordless_accounts(empty_db):
    """Revenir avant la connexion Google / GitHub : les comptes sans mot de passe sont conservés
    (condensé vide, refusé par verify_password) et la colonne redevient obligatoire."""
    cfg = alembic_config()
    command.upgrade(cfg, "head")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (email, name, password_hash, color) VALUES "
                          "('g@example.com', 'G', NULL, '#000'), ('p@example.com', 'P', 'h', '#000')"))
        conn.execute(text("INSERT INTO oauth_accounts (provider, subject, user_id) VALUES ('google', 's', 1)"))
    command.downgrade(cfg, "0001")
    engine.dispose()
    assert _rows("users") and [r[3] for r in _rows("users")] == ["", "h"]
    assert not [c for c in inspect(engine).get_columns("users") if c["name"] == "password_hash"][0]["nullable"]
    assert "oauth_accounts" not in inspect(engine).get_table_names()


# Schéma exact produit par l'ancien create_all (avant Alembic et la convention de nommage) :
# contraintes sans nom. Figé ici, car les modèles actuels ne savent plus le produire.
LEGACY_SQLITE_SCHEMA = """
CREATE TABLE users (
    id INTEGER NOT NULL, email VARCHAR(255) NOT NULL, name VARCHAR(80) NOT NULL,
    password_hash VARCHAR(255) NOT NULL, color VARCHAR(9) NOT NULL, created_at DATETIME,
    PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_users_email ON users (email);
CREATE TABLE boards (
    id INTEGER NOT NULL, name VARCHAR(120) NOT NULL, owner_id INTEGER NOT NULL,
    revision INTEGER NOT NULL, created_at DATETIME,
    PRIMARY KEY (id), FOREIGN KEY(owner_id) REFERENCES users (id)
);
CREATE TABLE memberships (
    id INTEGER NOT NULL, board_id INTEGER NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(10) NOT NULL,
    PRIMARY KEY (id), UNIQUE (board_id, user_id),
    FOREIGN KEY(board_id) REFERENCES boards (id) ON DELETE CASCADE, FOREIGN KEY(user_id) REFERENCES users (id)
);
CREATE INDEX ix_memberships_board_id ON memberships (board_id);
CREATE INDEX ix_memberships_user_id ON memberships (user_id);
CREATE TABLE columns (
    id INTEGER NOT NULL, board_id INTEGER NOT NULL, name VARCHAR(60) NOT NULL, position FLOAT NOT NULL,
    PRIMARY KEY (id), FOREIGN KEY(board_id) REFERENCES boards (id) ON DELETE CASCADE
);
CREATE INDEX ix_columns_board_id ON columns (board_id);
CREATE TABLE activities (
    id INTEGER NOT NULL, board_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    message VARCHAR(300) NOT NULL, created_at DATETIME,
    PRIMARY KEY (id), FOREIGN KEY(board_id) REFERENCES boards (id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users (id)
);
CREATE INDEX ix_activities_created_at ON activities (created_at);
CREATE INDEX ix_activities_board_id ON activities (board_id);
CREATE TABLE cards (
    id INTEGER NOT NULL, board_id INTEGER NOT NULL, column_id INTEGER NOT NULL, title VARCHAR(200) NOT NULL,
    description TEXT NOT NULL, position FLOAT NOT NULL, assignee_id INTEGER, version INTEGER NOT NULL,
    created_by_id INTEGER NOT NULL, created_at DATETIME, updated_at DATETIME,
    PRIMARY KEY (id), FOREIGN KEY(board_id) REFERENCES boards (id) ON DELETE CASCADE,
    FOREIGN KEY(column_id) REFERENCES columns (id) ON DELETE CASCADE,
    FOREIGN KEY(assignee_id) REFERENCES users (id) ON DELETE SET NULL,
    FOREIGN KEY(created_by_id) REFERENCES users (id)
);
CREATE INDEX ix_cards_column_id ON cards (column_id);
CREATE INDEX ix_cards_board_id ON cards (board_id);

INSERT INTO users VALUES (1, 'alice@example.com', 'Alice', 'h', '#6366f1', '2026-01-01 10:00:00');
INSERT INTO users VALUES (2, 'bob@example.com', 'Bob', 'h', '#0ea5e9', '2026-01-01 10:00:00');
INSERT INTO boards VALUES (1, 'Projet', 1, 3, '2026-01-01 10:00:00');
INSERT INTO memberships VALUES (1, 1, 1, 'owner');
INSERT INTO memberships VALUES (2, 1, 2, 'member');
INSERT INTO columns VALUES (1, 1, 'À faire', 1024.0);
INSERT INTO cards VALUES (1, 1, 1, 'Spec', 'Texte', 1536.5, 2, 4, 1, '2026-01-01 10:00:00', '2026-01-02 10:00:00');
INSERT INTO activities VALUES (1, 1, 1, 'a créé le tableau', '2026-01-01 10:00:00');
"""


def _rows(table, columns=None):
    selected = ", ".join(f'"{c}"' for c in columns) if columns else "*"
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(f'SELECT {selected} FROM "{table}" ORDER BY id'))]


@pytest.mark.skipif(not is_sqlite, reason="seules des bases SQLite locales ont été créées par create_all")
def test_database_created_before_alembic_is_upgraded_in_place(empty_db):
    """Base locale créée par l'ancien create_all : pas d'erreur « table already exists », données
    conservées, et contraintes renommées selon la convention, sinon la prochaine autogénération
    produirait de fausses différences et les migrations visant ces noms échoueraient."""
    raw = engine.raw_connection()
    try:
        raw.driver_connection.executescript(LEGACY_SQLITE_SCHEMA)
    finally:
        raw.close()
    engine.dispose()
    # Schéma 0001 : sans les tables ni les colonnes ajoutées depuis, comparées à part
    legacy = {t: [c["name"] for c in inspect(engine).get_columns(t)] for t in inspect(engine).get_table_names()}
    before = {t: _rows(t, columns) for t, columns in legacy.items()}
    assert inspect(engine).get_unique_constraints("memberships")[0]["name"] is None

    upgrade_schema()
    engine.dispose()

    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    with engine.connect() as conn:
        assert MigrationContext.configure(conn).get_current_revision() == head
        assert compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata) == []
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    assert {t: _rows(t, columns) for t, columns in legacy.items()} == before
    assert [r[0] for r in _rows("users", ["token_version"])] == [0, 0]

    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        assert insp.get_pk_constraint(table.name)["name"] == table.primary_key.name
        assert {fk["name"] for fk in insp.get_foreign_keys(table.name)} == {
            fk.name for fk in table.foreign_key_constraints
        }
        assert {ix["name"] for ix in insp.get_indexes(table.name)} == {ix.name for ix in table.indexes}
    assert [u["name"] for u in insp.get_unique_constraints("memberships")] == ["uq_memberships_board_id_user_id"]

    # Les ON DELETE CASCADE fonctionnent toujours après la recréation des tables
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM boards WHERE id = 1"))
    assert [_rows(t) for t in ("memberships", "columns", "cards", "activities")] == [[], [], [], []]
    assert len(_rows("users")) == 2


def test_postgres_urls_use_psycopg3():
    assert normalize_url("postgresql://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    assert normalize_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("sqlite:///./tandem.db") == "sqlite:///./tandem.db"
