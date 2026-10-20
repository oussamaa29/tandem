import os
import sys
import tempfile
from pathlib import Path

import pytest

# Postgres en CI (TEST_DATABASE_URL), sinon un fichier SQLite temporaire : aucune installation requise
if os.environ.get("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
else:
    _db_dir = tempfile.mkdtemp()
    os.environ["DATABASE_URL"] = f"sqlite:///{_db_dir}/test.db"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from database import Base, engine, is_sqlite, upgrade_schema  # noqa: E402
from main import app  # noqa: E402


def drop_schema():
    """Supprime toutes les tables, y compris celle des versions d'Alembic."""
    Base.metadata.drop_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture(scope="session", autouse=True)
def schema():
    # Les tests tournent sur le schéma produit par les migrations, pas par create_all. Une base
    # Postgres réutilisée peut contenir le schéma d'une exécution précédente : on repart de zéro.
    drop_schema()
    upgrade_schema()
    yield


@pytest.fixture(autouse=True)
def fresh_db(schema):
    # Vider plutôt que recréer : rapide, et identique sur les deux bases (identifiants repartant à 1)
    with engine.begin() as conn:
        if is_sqlite:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())
        else:
            quote = engine.dialect.identifier_preparer.quote
            tables = ", ".join(quote(t.name) for t in Base.metadata.sorted_tables)
            conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


class Account:
    def __init__(self, client, name, email):
        r = client.post("/api/auth/register", json={"email": email, "name": name, "password": "motdepasse123"})
        assert r.status_code == 201, r.text
        self.token = r.json()["token"]
        self.user = r.json()["user"]
        self.id = self.user["id"]
        self.h = {"Authorization": f"Bearer {self.token}"}


@pytest.fixture
def alice(client):
    return Account(client, "Alice", "alice@example.com")


@pytest.fixture
def bob(client):
    return Account(client, "Bob", "bob@example.com")


@pytest.fixture
def board(client, alice):
    r = client.post("/api/boards", json={"name": "Projet"}, headers=alice.h)
    assert r.status_code == 201
    return client.get(f"/api/boards/{r.json()['id']}", headers=alice.h).json()


@pytest.fixture
def shared_board(client, alice, bob, board):
    r = client.post(f"/api/boards/{board['id']}/members", json={"email": "bob@example.com"}, headers=alice.h)
    assert r.status_code == 201, r.text
    return client.get(f"/api/boards/{board['id']}", headers=alice.h).json()


def add_card(client, account, column_id, title="Tâche"):
    r = client.post(f"/api/columns/{column_id}/cards", json={"title": title}, headers=account.h)
    assert r.status_code == 201, r.text
    return r.json()


def receive_until(ws, event_type, limit=10):
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["type"] == event_type:
            return msg
    raise AssertionError(f"pas d'événement {event_type}")
