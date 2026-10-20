from conftest import add_card


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
