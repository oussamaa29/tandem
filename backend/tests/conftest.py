import os
import sys
import tempfile
from pathlib import Path

import pytest

_db_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_db_dir}/test.db"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from database import Base, engine  # noqa: E402
from main import app  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
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
