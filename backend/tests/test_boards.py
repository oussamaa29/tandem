import threading
import time
from contextlib import contextmanager

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

import main
from conftest import add_card
from database import engine, is_sqlite


@contextmanager
def before_first_flush(sql, params):
    """Exécute `sql` dans une autre connexion juste avant la première écriture de la requête :
    simule une requête concurrente validée entre nos vérifications et notre écriture."""
    fired = []

    @event.listens_for(Session, "before_flush")
    def concurrent_write(session, _ctx, _instances):
        if fired:
            return
        fired.append(True)
        with engine.begin() as conn:
            conn.execute(text(sql), params)

    try:
        yield fired
    finally:
        event.remove(Session, "before_flush", concurrent_write)


def test_new_board_has_default_columns_and_owner(client, alice, board):
    assert [c["name"] for c in board["columns"]] == ["À faire", "En cours", "Terminé"]
    assert board["role"] == "owner"
    assert board["members"][0]["id"] == alice.id
    assert board["revision"] == 0


def test_boards_are_private(client, alice, bob, board):
    assert client.get(f"/api/boards/{board['id']}", headers=bob.h).status_code == 404
    assert client.get("/api/boards", headers=bob.h).json() == []
    col = board["columns"][0]["id"]
    assert client.post(f"/api/columns/{col}/cards", json={"title": "x"}, headers=bob.h).status_code == 404


def test_invite_requires_existing_account_and_owner(client, alice, bob, board):
    url = f"/api/boards/{board['id']}/members"
    assert client.post(url, json={"email": "inconnu@example.com"}, headers=alice.h).status_code == 404
    assert client.post(url, json={"email": "bob@example.com"}, headers=alice.h).status_code == 201
    assert client.post(url, json={"email": "bob@example.com"}, headers=alice.h).status_code == 409
    # Un membre ne peut pas inviter
    client.post("/api/auth/register", json={"email": "carol@example.com", "name": "Carol", "password": "motdepasse123"})
    assert client.post(url, json={"email": "carol@example.com"}, headers=bob.h).status_code == 403
    assert [b["name"] for b in client.get("/api/boards", headers=bob.h).json()] == ["Projet"]


def test_only_owner_can_delete_board(client, alice, bob, shared_board):
    url = f"/api/boards/{shared_board['id']}"
    assert client.delete(url, headers=bob.h).status_code == 403
    assert client.delete(url, headers=alice.h).status_code == 204
    assert client.get(url, headers=alice.h).status_code == 404


def test_removing_member_unassigns_their_cards(client, alice, bob, shared_board):
    col = shared_board["columns"][0]["id"]
    card = add_card(client, alice, col)
    r = client.patch(f"/api/cards/{card['id']}", json={"version": card["version"], "assignee_id": bob.id}, headers=alice.h)
    assert r.json()["assignee_id"] == bob.id

    assert client.delete(f"/api/boards/{shared_board['id']}/members/{bob.id}", headers=alice.h).status_code == 204
    board = client.get(f"/api/boards/{shared_board['id']}", headers=alice.h).json()
    assert board["columns"][0]["cards"][0]["assignee_id"] is None
    # La libération compte comme une modification : un brouillon basé sur l'ancienne version donnera 409
    assert board["columns"][0]["cards"][0]["version"] == r.json()["version"] + 1
    assert client.get(f"/api/boards/{shared_board['id']}", headers=bob.h).status_code == 404


def test_concurrent_invite_of_same_person_is_409(client, alice, bob, board):
    url = f"/api/boards/{board['id']}/members"
    with before_first_flush("INSERT INTO memberships (board_id, user_id, role) VALUES (:b, :u, 'member')",
                            {"b": board["id"], "u": bob.id}) as fired:
        r = client.post(url, json={"email": "bob@example.com"}, headers=alice.h)
    assert fired
    assert r.status_code == 409
    members = client.get(f"/api/boards/{board['id']}", headers=alice.h).json()["members"]
    assert [m["id"] for m in members].count(bob.id) == 1


def test_invite_into_board_deleted_concurrently_is_404(client, alice, bob, board, monkeypatch):
    """Clé étrangère violée (tableau supprimé entre la vérification et l'insertion) : 404, et non
    le 409 « fait déjà partie du tableau ». En Postgres lock_board empêche cette course ; on le
    neutralise pour tester le cas SQLite, où il ne verrouille rien."""
    monkeypatch.setattr(main, "lock_board", lambda db, board_id: None)
    with before_first_flush("DELETE FROM boards WHERE id = :b", {"b": board["id"]}) as fired:
        r = client.post(f"/api/boards/{board['id']}/members", json={"email": "bob@example.com"}, headers=alice.h)
    assert fired
    assert r.status_code == 404


def test_write_on_board_deleted_before_lock_is_404(client, alice, board, monkeypatch):
    original = main.board_for

    def board_for_then_deleted(db, user, board_id, owner_only=False):
        result = original(db, user, board_id, owner_only)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM boards WHERE id = :b"), {"b": board_id})
        return result

    monkeypatch.setattr(main, "board_for", board_for_then_deleted)
    r = client.post(f"/api/boards/{board['id']}/columns", json={"name": "Revue"}, headers=alice.h)
    assert r.status_code == 404


@pytest.mark.skipif(is_sqlite, reason="verrous de ligne Postgres ; SQLite verrouille la base entière")
def test_deleting_board_during_card_edit_does_not_deadlock(client, alice, board, monkeypatch):
    """L'édition a écrit sa carte et n'a pas encore commité quand la suppression du tableau arrive.
    Avant lock_board : l'édition tenait la carte et attendait le tableau, la suppression tenait le
    tableau et attendait la carte (ON DELETE CASCADE) → interblocage détecté par Postgres, 500."""
    card = add_card(client, alice, board["columns"][0]["id"], "Spec")
    card_written = threading.Event()
    original_emit = main.emit

    def slow_emit(*args, **kwargs):
        card_written.set()  # carte modifiée, transaction encore ouverte
        time.sleep(0.5)  # laisse à la suppression le temps d'arriver sur ses verrous
        return original_emit(*args, **kwargs)

    monkeypatch.setattr(main, "emit", slow_emit)
    statuses = {}

    def edit():
        r = client.patch(f"/api/cards/{card['id']}", json={"version": 1, "description": "x"}, headers=alice.h)
        statuses["edit"] = r.status_code

    def delete():
        assert card_written.wait(timeout=5)
        statuses["delete"] = client.delete(f"/api/boards/{board['id']}", headers=alice.h).status_code

    threads = [threading.Thread(target=edit), threading.Thread(target=delete)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert statuses == {"edit": 200, "delete": 204}


def test_member_can_leave_but_owner_cannot(client, alice, bob, shared_board):
    base = f"/api/boards/{shared_board['id']}/members"
    assert client.delete(f"{base}/{alice.id}", headers=alice.h).status_code == 409
    assert client.delete(f"{base}/{bob.id}", headers=bob.h).status_code == 204


def test_every_change_bumps_revision_and_logs_activity(client, alice, board):
    col = board["columns"][0]["id"]
    card = add_card(client, alice, col, "Maquette")
    client.post(f"/api/cards/{card['id']}/move", json={"column_id": board["columns"][1]["id"], "index": 0}, headers=alice.h)

    fresh = client.get(f"/api/boards/{board['id']}", headers=alice.h).json()
    assert fresh["revision"] == 2
    messages = [a["message"] for a in client.get(f"/api/boards/{board['id']}/activity", headers=alice.h).json()]
    assert messages[0] == "a déplacé « Maquette » vers En cours"
    assert messages[-1] == "a créé le tableau"


def test_columns_can_be_added_renamed_reordered_and_deleted(client, alice, board):
    r = client.post(f"/api/boards/{board['id']}/columns", json={"name": "Revue"}, headers=alice.h)
    col = r.json()
    client.patch(f"/api/columns/{col['id']}", json={"name": "Relecture"}, headers=alice.h)
    client.post(f"/api/columns/{col['id']}/move", json={"index": 0}, headers=alice.h)
    names = [c["name"] for c in client.get(f"/api/boards/{board['id']}", headers=alice.h).json()["columns"]]
    assert names == ["Relecture", "À faire", "En cours", "Terminé"]

    add_card(client, alice, col["id"])
    assert client.delete(f"/api/columns/{col['id']}", headers=alice.h).status_code == 204
    fresh = client.get(f"/api/boards/{board['id']}", headers=alice.h).json()
    assert len(fresh["columns"]) == 3


def test_blank_names_are_rejected(client, alice, board):
    col = board["columns"][0]["id"]
    assert client.post(f"/api/columns/{col}/cards", json={"title": "   "}, headers=alice.h).status_code == 422
    assert client.post("/api/boards", json={"name": ""}, headers=alice.h).status_code == 422
