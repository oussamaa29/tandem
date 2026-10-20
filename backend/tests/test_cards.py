import threading

from sqlalchemy import event, text
from sqlalchemy.orm import Session

import main
from conftest import add_card
from database import engine


def titles(client, account, board_id):
    board = client.get(f"/api/boards/{board_id}", headers=account.h).json()
    return [[c["title"] for c in col["cards"]] for col in board["columns"]]


def test_cards_are_appended_in_order(client, alice, board):
    col = board["columns"][0]["id"]
    for t in ("A", "B", "C"):
        add_card(client, alice, col, t)
    assert titles(client, alice, board["id"])[0] == ["A", "B", "C"]


def test_move_within_and_across_columns(client, alice, board):
    todo, doing = board["columns"][0]["id"], board["columns"][1]["id"]
    a, b, c = (add_card(client, alice, todo, t) for t in ("A", "B", "C"))

    client.post(f"/api/cards/{c['id']}/move", json={"column_id": todo, "index": 0}, headers=alice.h)
    assert titles(client, alice, board["id"])[0] == ["C", "A", "B"]

    client.post(f"/api/cards/{a['id']}/move", json={"column_id": doing, "index": 0}, headers=alice.h)
    client.post(f"/api/cards/{b['id']}/move", json={"column_id": doing, "index": 0}, headers=alice.h)
    assert titles(client, alice, board["id"])[:2] == [["C"], ["B", "A"]]


def test_move_rebalances_when_positions_are_exhausted(client, alice, board):
    todo = board["columns"][0]["id"]
    first = add_card(client, alice, todo, "first")
    last = add_card(client, alice, todo, "last")
    # Insérer toujours juste après « first » finit par épuiser l'écart entre les deux
    inserted = []
    rebalanced_seen = False
    for i in range(60):
        card = add_card(client, alice, todo, f"n{i}")
        r = client.post(f"/api/cards/{card['id']}/move", json={"column_id": todo, "index": 1}, headers=alice.h)
        assert r.status_code == 200
        inserted.insert(0, f"n{i}")
        rebalanced_seen = rebalanced_seen or r.json()["position"] % 1024 == 0
    order = titles(client, alice, board["id"])[0]
    assert order == ["first", *inserted, "last"]
    assert rebalanced_seen
    assert first and last


def test_cannot_move_card_to_another_board(client, alice, board):
    other = client.post("/api/boards", json={"name": "Autre"}, headers=alice.h).json()
    other_col = client.get(f"/api/boards/{other['id']}", headers=alice.h).json()["columns"][0]["id"]
    card = add_card(client, alice, board["columns"][0]["id"])
    r = client.post(f"/api/cards/{card['id']}/move", json={"column_id": other_col, "index": 0}, headers=alice.h)
    assert r.status_code == 404


def test_optimistic_locking_rejects_stale_edits(client, alice, bob, shared_board):
    card = add_card(client, alice, shared_board["columns"][0]["id"], "Spec")
    url = f"/api/cards/{card['id']}"

    r = client.patch(url, json={"version": 1, "description": "Version d'Alice"}, headers=alice.h)
    assert r.status_code == 200 and r.json()["version"] == 2

    # Bob éditait la version 1 : son enregistrement est refusé, avec la version à jour
    r = client.patch(url, json={"version": 1, "description": "Version de Bob"}, headers=bob.h)
    assert r.status_code == 409
    assert r.json()["detail"]["card"]["description"] == "Version d'Alice"

    r = client.patch(url, json={"version": 2, "description": "Version de Bob"}, headers=bob.h)
    assert r.status_code == 200 and r.json()["version"] == 3


def test_optimistic_lock_is_atomic(client, alice, bob, shared_board, monkeypatch):
    """Une écriture concurrente validée APRÈS notre lecture mais AVANT notre écriture doit aussi
    donner 409 : la vérification de version se fait dans l'UPDATE, pas seulement en Python."""
    card = add_card(client, alice, shared_board["columns"][0]["id"], "Spec")
    original = main.card_for

    def card_for_then_concurrent_edit(db, user, card_id):
        result = original(db, user, card_id)
        assert result[0].version == 1  # lu par la requête…
        # …puis Bob enregistre avant que la requête n'écrive
        with engine.begin() as conn:
            conn.execute(text("UPDATE cards SET description = 'Version de Bob', version = 2 WHERE id = :id"),
                         {"id": card_id})
        return result

    monkeypatch.setattr(main, "card_for", card_for_then_concurrent_edit)
    r = client.patch(f"/api/cards/{card['id']}", json={"version": 1, "description": "Version d'Alice"}, headers=alice.h)
    assert r.status_code == 409
    assert r.json()["detail"]["card"]["description"] == "Version de Bob"
    monkeypatch.setattr(main, "card_for", original)
    assert titles(client, alice, shared_board["id"])[0] == ["Spec"]
    fresh = client.get(f"/api/boards/{shared_board['id']}", headers=alice.h).json()["columns"][0]["cards"][0]
    assert (fresh["description"], fresh["version"]) == ("Version de Bob", 2)


def test_concurrent_edits_with_same_version_one_wins(client, alice, bob, shared_board, monkeypatch):
    """Deux requêtes réellement simultanées avec la même version : une seule est acceptée."""
    card = add_card(client, alice, shared_board["columns"][0]["id"], "Spec")
    barrier = threading.Barrier(2)
    original = main.card_for

    def synchronized_card_for(db, user, card_id):
        result = original(db, user, card_id)
        _ = result[0].version
        barrier.wait(timeout=5)  # les deux requêtes ont lu la version 1 avant d'écrire
        return result

    monkeypatch.setattr(main, "card_for", synchronized_card_for)
    statuses = {}

    def edit(account, text_):
        r = client.patch(f"/api/cards/{card['id']}", json={"version": 1, "description": text_}, headers=account.h)
        statuses[account.user["name"]] = r.status_code

    threads = [threading.Thread(target=edit, args=(a, f"Version de {a.user['name']}")) for a in (alice, bob)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(statuses.values()) == [200, 409]


def test_null_description_is_stored_as_empty(client, alice, board):
    card = add_card(client, alice, board["columns"][0]["id"])
    r = client.patch(f"/api/cards/{card['id']}", json={"version": 1, "description": "Texte"}, headers=alice.h)
    r = client.patch(f"/api/cards/{card['id']}", json={"version": 2, "description": None}, headers=alice.h)
    assert r.status_code == 200
    assert r.json()["description"] == "" and r.json()["version"] == 3


def test_event_payload_survives_concurrent_delete_after_commit(client, alice, board):
    """Le contenu de l'événement est construit avant le commit : une suppression concurrente
    juste après ne transforme pas une écriture réussie en erreur 500."""
    card = add_card(client, alice, board["columns"][0]["id"], "Avant")
    fired = []

    @event.listens_for(Session, "after_commit")
    def delete_right_after_commit(session):
        if fired:
            return
        fired.append(True)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM cards WHERE id = :id"), {"id": card["id"]})

    try:
        r = client.patch(f"/api/cards/{card['id']}", json={"version": 1, "title": "Après"}, headers=alice.h)
    finally:
        event.remove(Session, "after_commit", delete_right_after_commit)
    assert fired
    assert r.status_code == 200 and r.json()["title"] == "Après"


def test_tied_positions_are_ordered_by_id_and_rebalanced(client, alice, board):
    """Deux déplacements simultanés peuvent produire la même position : l'ordre reste
    déterministe (départage par id) et une insertion entre les deux rééquilibre la colonne."""
    todo = board["columns"][0]["id"]
    a, b, c = (add_card(client, alice, todo, t) for t in ("A", "B", "C"))
    with engine.begin() as conn:
        conn.execute(text("UPDATE cards SET position = 2048 WHERE id IN (:a, :b)"), {"a": a["id"], "b": b["id"]})
    assert titles(client, alice, board["id"])[0] == ["A", "B", "C"]

    r = client.post(f"/api/cards/{c['id']}/move", json={"column_id": todo, "index": 1}, headers=alice.h)
    assert r.status_code == 200
    assert titles(client, alice, board["id"])[0] == ["A", "C", "B"]
    cards = client.get(f"/api/boards/{board['id']}", headers=alice.h).json()["columns"][0]["cards"]
    assert len({card["position"] for card in cards}) == 3


def test_noop_update_does_not_bump_version(client, alice, board):
    card = add_card(client, alice, board["columns"][0]["id"], "Same")
    r = client.patch(f"/api/cards/{card['id']}", json={"version": 1, "title": "Same"}, headers=alice.h)
    assert r.json()["version"] == 1


def test_assignee_must_be_a_member(client, alice, bob, board):
    card = add_card(client, alice, board["columns"][0]["id"])
    r = client.patch(f"/api/cards/{card['id']}", json={"version": 1, "assignee_id": bob.id}, headers=alice.h)
    assert r.status_code == 400


def test_delete_card(client, alice, board):
    card = add_card(client, alice, board["columns"][0]["id"])
    assert client.delete(f"/api/cards/{card['id']}", headers=alice.h).status_code == 204
    assert titles(client, alice, board["id"])[0] == []
