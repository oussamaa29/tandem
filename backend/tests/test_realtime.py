from contextlib import contextmanager

import pytest
from starlette.websockets import WebSocketDisconnect

import main
import models
from conftest import add_card, receive_until
from database import SessionLocal


@contextmanager
def board_ws(client, board_id, account):
    """Ouvre le WebSocket d'un tableau et s'authentifie par le premier message."""
    with client.websocket_connect(f"/ws/boards/{board_id}") as ws:
        ws.send_json({"type": "auth", "token": account.token})
        yield ws


def close_code(ws):
    with pytest.raises(WebSocketDisconnect) as exc:
        for _ in range(10):
            ws.receive_json()
    return exc.value.code


def test_rejects_missing_token_and_non_members(client, alice, bob, board):
    url = f"/ws/boards/{board['id']}"
    with client.websocket_connect(url) as ws:
        ws.send_json({"type": "auth", "token": "pas-un-jeton"})
        assert close_code(ws) == 4401

    # Le premier message doit être l'authentification
    with client.websocket_connect(url) as ws:
        ws.send_json({"type": "ping"})
        assert close_code(ws) == 4401

    with board_ws(client, board["id"], bob) as ws:
        assert close_code(ws) == 4403


def test_auth_times_out_and_token_in_url_is_ignored(client, alice, board, monkeypatch):
    monkeypatch.setattr(main, "WS_AUTH_TIMEOUT", 0.2)
    # Le jeton en query string n'est plus lu : il finirait dans les journaux d'accès
    with client.websocket_connect(f"/ws/boards/{board['id']}?token={alice.token}") as ws:
        assert close_code(ws) == 4401


def test_changes_are_broadcast_to_other_members(client, alice, bob, shared_board):
    with board_ws(client, shared_board["id"], bob) as bob_ws:
        receive_until(bob_ws, "presence")

        card = add_card(client, alice, shared_board["columns"][0]["id"], "Écrire les tests")
        event = receive_until(bob_ws, "card.created")
        assert event["payload"]["id"] == card["id"]
        assert event["actor_id"] == alice.id
        assert event["revision"] == shared_board["revision"] + 1
        assert event["activity"]["message"] == "a ajouté « Écrire les tests » dans À faire"

        target = shared_board["columns"][2]["id"]
        client.post(f"/api/cards/{card['id']}/move", json={"column_id": target, "index": 0}, headers=alice.h)
        event = receive_until(bob_ws, "card.moved")
        assert event["payload"]["card"]["column_id"] == target
        assert event["revision"] == shared_board["revision"] + 2


def test_presence_and_editing_indicator(client, alice, bob, shared_board):
    with board_ws(client, shared_board["id"], alice) as alice_ws:
        assert [u["name"] for u in receive_until(alice_ws, "presence")["users"]] == ["Alice"]

        with board_ws(client, shared_board["id"], bob) as bob_ws:
            users = receive_until(alice_ws, "presence")["users"]
            assert sorted(u["name"] for u in users) == ["Alice", "Bob"]

            bob_ws.send_json({"type": "editing", "card_id": 42})
            users = receive_until(alice_ws, "presence")["users"]
            assert next(u for u in users if u["name"] == "Bob")["editing_card_id"] == 42

        # Bob se déconnecte : il disparaît de la présence
        users = receive_until(alice_ws, "presence")["users"]
        assert [u["name"] for u in users] == ["Alice"]


def test_removed_member_is_disconnected(client, alice, bob, shared_board):
    with board_ws(client, shared_board["id"], bob) as bob_ws:
        receive_until(bob_ws, "presence")
        client.delete(f"/api/boards/{shared_board['id']}/members/{bob.id}", headers=alice.h)
        receive_until(bob_ws, "member.removed")
        with pytest.raises(WebSocketDisconnect) as exc:
            bob_ws.receive_json()
        assert exc.value.code == 4403


def test_member_removed_while_joining_is_disconnected(client, alice, bob, shared_board, monkeypatch):
    """Retrait validé entre le contrôle d'appartenance et l'entrée dans la room : hub.kick ne
    voyait pas encore la connexion, le second contrôle après join doit la fermer."""
    original_join = main.hub.join

    async def join_after_removal(board_id, ws, *identity):
        db = SessionLocal()
        try:
            db.query(models.Membership).filter_by(board_id=board_id, user_id=bob.id).delete()
            db.commit()
        finally:
            db.close()
        return await original_join(board_id, ws, *identity)

    monkeypatch.setattr(main.hub, "join", join_after_removal)
    with board_ws(client, shared_board["id"], bob) as bob_ws:
        assert close_code(bob_ws) == 4403
    assert main.hub.peers(shared_board["id"]) == []


def test_ping_pong(client, alice, board):
    with board_ws(client, board["id"], alice) as ws:
        receive_until(ws, "presence")
        ws.send_json({"type": "ping"})
        assert receive_until(ws, "pong")["type"] == "pong"
