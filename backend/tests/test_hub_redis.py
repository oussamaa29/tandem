"""Plusieurs instances du hub reliées par Redis : chaque Hub ci-dessous joue le rôle d'un processus
serveur distinct (connexions, abonnement et identifiant propres), seul Redis est partagé.

Redis réel si TEST_REDIS_URL est défini (CI), sinon fakeredis en mémoire. Les coupures de Redis
ne sont simulées qu'avec fakeredis."""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager

import fakeredis
import pytest
import redis.asyncio as aioredis
from redis.asyncio.client import PubSub
from redis.exceptions import ConnectionError as RedisConnectionError

import hub as hub_module
from hub import Hub

REAL_REDIS_URL = os.environ.get("TEST_REDIS_URL")


class FakeWS:
    """WebSocket minimal : mémorise les messages envoyés et le code de fermeture."""

    def __init__(self):
        self.sent: list[dict] = []
        self.closed: int | None = None

    async def send_json(self, message):
        if self.closed is not None:
            raise RuntimeError("connexion fermée")
        self.sent.append(message)

    async def close(self, code=1000):
        self.closed = code

    def of_type(self, kind):
        return [m for m in self.sent if m["type"] == kind]

    def users(self):
        """Dernière présence reçue : {nom: carte en cours d'édition}."""
        presence = self.of_type("presence")
        return {u["name"]: u["editing_card_id"] for u in presence[-1]["users"]} if presence else None


async def until(condition, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition jamais remplie")
        await asyncio.sleep(0.02)


def run(coro):
    asyncio.run(asyncio.wait_for(coro, timeout=20))


@asynccontextmanager
async def cluster(count=2):
    """`count` instances partageant un Redis, battement et TTL raccourcis pour les tests."""
    server = fakeredis.FakeServer()
    prefix = f"test-{uuid.uuid4().hex[:8]}"  # isole les tests sur un vrai Redis partagé
    clients = [
        aioredis.from_url(REAL_REDIS_URL, decode_responses=True) if REAL_REDIS_URL
        else fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
        for _ in range(count)
    ]
    hubs = [
        Hub(redis_client=c, prefix=prefix, heartbeat=0.1, instance_ttl=0.6, retry_min=0.05, retry_max=0.2)
        for c in clients
    ]
    for h in hubs:
        await h.start()
    await until(lambda: all(h._subscribed for h in hubs))
    try:
        yield hubs, server
    finally:
        for h in hubs:
            await h.stop()
        for c in clients:
            await c.aclose()


BOARD = 1


def test_event_published_on_one_instance_reaches_the_others_once_and_in_order():
    async def scenario():
        async with cluster() as ((a, b), _):
            ws_a, ws_b = FakeWS(), FakeWS()
            await a.join(BOARD, ws_a, 1, "Alice", "#111")
            await b.join(BOARD, ws_b, 2, "Bob", "#222")
            for revision in range(1, 21):
                await b.publish(BOARD, {"type": "card.moved", "revision": revision})
            await until(lambda: len(ws_a.of_type("card.moved")) == 20 and len(ws_b.of_type("card.moved")) == 20)
            await asyncio.sleep(0.2)
            # Une seule réception par connexion (y compris sur l'instance qui publie), dans l'ordre
            for ws in (ws_a, ws_b):
                assert [m["revision"] for m in ws.of_type("card.moved")] == list(range(1, 21))
            # Un autre tableau n'est pas concerné
            await a.publish(BOARD + 1, {"type": "card.moved", "revision": 1})
            await asyncio.sleep(0.2)
            assert len(ws_b.of_type("card.moved")) == 20

    run(scenario())


def test_presence_is_merged_across_instances_and_deduplicated_per_user():
    async def scenario():
        async with cluster() as ((a, b), _):
            alice_a, bob_b, alice_b = FakeWS(), FakeWS(), FakeWS()
            await a.join(BOARD, alice_a, 1, "Alice", "#111")
            await b.join(BOARD, bob_b, 2, "Bob", "#222")
            await until(lambda: alice_a.users() == {"Alice": None, "Bob": None})
            await until(lambda: bob_b.users() == {"Alice": None, "Bob": None})

            # Second onglet d'Alice, sur l'autre instance : elle n'apparaît toujours qu'une fois
            await b.join(BOARD, alice_b, 1, "Alice", "#111")
            await until(lambda: alice_b.users() == {"Alice": None, "Bob": None})
            assert [u["name"] for u in alice_b.of_type("presence")[-1]["users"]].count("Alice") == 1

            await b.leave(BOARD, bob_b)
            await until(lambda: alice_a.users() == {"Alice": None})
            # Alice ferme l'onglet de l'instance B : toujours présente grâce à celui de A
            await b.leave(BOARD, alice_b)
            await asyncio.sleep(0.3)
            assert alice_a.users() == {"Alice": None}

    run(scenario())


def test_editing_indicator_crosses_instances():
    async def scenario():
        async with cluster() as ((a, b), _):
            alice_a, bob_b, alice_b = FakeWS(), FakeWS(), FakeWS()
            await a.join(BOARD, alice_a, 1, "Alice", "#111")
            await b.join(BOARD, bob_b, 2, "Bob", "#222")
            await b.set_editing(BOARD, bob_b, 42)
            await until(lambda: alice_a.users() == {"Alice": None, "Bob": 42})

            # Alice modifie dans un onglet (instance B) mais pas dans l'autre (instance A)
            await b.join(BOARD, alice_b, 1, "Alice", "#111")
            await b.set_editing(BOARD, alice_b, 7)
            await until(lambda: bob_b.users() == {"Alice": 7, "Bob": 42})
            await until(lambda: alice_a.users() == {"Alice": 7, "Bob": 42})

            await b.set_editing(BOARD, bob_b, None)
            await until(lambda: alice_a.users() == {"Alice": 7, "Bob": None})

    run(scenario())


def test_kick_closes_the_member_connections_on_every_instance():
    async def scenario():
        async with cluster() as ((a, b), _):
            alice_a, bob_a, bob_b = FakeWS(), FakeWS(), FakeWS()
            await a.join(BOARD, alice_a, 1, "Alice", "#111")
            await a.join(BOARD, bob_a, 2, "Bob", "#222")
            await b.join(BOARD, bob_b, 2, "Bob", "#222")
            await until(lambda: alice_a.users() == {"Alice": None, "Bob": None})

            await a.kick(BOARD, 2)
            await until(lambda: bob_a.closed == 4403 and bob_b.closed == 4403)
            assert b.peers(BOARD) == [] and [p.user_id for p in a.peers(BOARD)] == [1]
            await until(lambda: alice_a.users() == {"Alice": None})

    run(scenario())


def test_close_room_closes_connections_on_every_instance():
    async def scenario():
        async with cluster() as ((a, b), _):
            alice_a, bob_b = FakeWS(), FakeWS()
            await a.join(BOARD, alice_a, 1, "Alice", "#111")
            await b.join(BOARD, bob_b, 2, "Bob", "#222")
            await b.publish(BOARD, {"type": "board.deleted", "payload": {}})
            await b.close_room(BOARD)
            await until(lambda: alice_a.closed == 4404 and bob_b.closed == 4404)
            # L'événement de suppression est arrivé avant la fermeture
            assert alice_a.of_type("board.deleted")
            assert a.rooms == {} and b.rooms == {}

    run(scenario())


def test_crashed_instance_users_disappear_from_presence():
    async def scenario():
        async with cluster() as ((a, b), _):
            alice_a, bob_b = FakeWS(), FakeWS()
            await a.join(BOARD, alice_a, 1, "Alice", "#111")
            await b.join(BOARD, bob_b, 2, "Bob", "#222")
            await until(lambda: alice_a.users() == {"Alice": None, "Bob": None})

            # Arrêt brutal de B : plus de battement ni d'abonnement, et aucun nettoyage
            for task in b._tasks:
                task.cancel()
            await asyncio.gather(*b._tasks, return_exceptions=True)
            b._tasks, b._redis = [], None

            # Sa clé de vie expire (TTL 0,6 s) ; le battement de A le retire et diffuse la présence
            await until(lambda: alice_a.users() == {"Alice": None}, timeout=5)

    run(scenario())


@pytest.mark.skipif(bool(REAL_REDIS_URL), reason="coupure simulée avec fakeredis uniquement")
def test_redis_outage_degrades_to_local_delivery_then_recovers():
    async def scenario():
        async with cluster() as ((a, b), server):
            alice_a, bob_a, carol_b = FakeWS(), FakeWS(), FakeWS()
            await a.join(BOARD, alice_a, 1, "Alice", "#111")
            await b.join(BOARD, carol_b, 3, "Carol", "#333")

            server.connected = False
            # Les connexions de la même instance continuent de recevoir événements et présence
            await a.publish(BOARD, {"type": "card.created", "revision": 1})
            await until(lambda: len(alice_a.of_type("card.created")) == 1)
            await a.join(BOARD, bob_a, 2, "Bob", "#222")
            await until(lambda: set(bob_a.users() or {}) >= {"Alice", "Bob"})
            await a.kick(BOARD, 2)
            await until(lambda: bob_a.closed == 4403)
            assert not carol_b.of_type("card.created")  # perdu pour B : rattrapé via les révisions

            # Retour de Redis : l'abonnement se reconnecte (délai exponentiel) et tout repart
            server.connected = True
            await until(lambda: a._subscribed and b._subscribed, timeout=5)
            await until(lambda: carol_b.users() == {"Alice": None, "Carol": None}, timeout=5)
            await a.publish(BOARD, {"type": "card.created", "revision": 2})
            await until(lambda: len(carol_b.of_type("card.created")) == 1)
            await asyncio.sleep(0.2)
            assert [m["revision"] for m in alice_a.of_type("card.created")] == [1, 2]  # pas de doublon

    run(scenario())


def test_without_redis_url_the_hub_stays_in_memory():
    async def scenario():
        local = Hub()
        await local.start()
        ws = FakeWS()
        await local.join(BOARD, ws, 1, "Alice", "#111")
        await local.publish(BOARD, {"type": "card.created", "revision": 1})
        await until(lambda: len(ws.of_type("card.created")) == 1)
        assert local._tasks == [] and ws.users() == {"Alice": None}
        await local.stop()

    run(scenario())


@pytest.mark.skipif(bool(REAL_REDIS_URL), reason="coupure de l'abonnement simulée avec fakeredis uniquement")
def test_kick_and_deletion_missed_during_subscription_outage_are_caught_on_resubscribe(monkeypatch):
    """Seul l'abonnement de B tombe (Redis reste joignable : A publie normalement, sans repli
    local). Le retrait de Bob et la suppression d'un tableau publiés pendant la coupure sont
    perdus pour B : la revalidation auprès de la base au réabonnement les rattrape."""
    async def scenario():
        members = {BOARD: {1, 2}, BOARD + 1: {2}}
        revalidations = []

        async def revalidate(connected):
            revalidations.append(connected)
            return {board_id: members.get(board_id) for board_id in connected}

        async with cluster() as ((a, b), _):
            a.revalidate = b.revalidate = revalidate
            alice_a, bob_b, bob_b2 = FakeWS(), FakeWS(), FakeWS()
            await a.join(BOARD, alice_a, 1, "Alice", "#111")
            await b.join(BOARD, bob_b, 2, "Bob", "#222")
            await b.join(BOARD + 1, bob_b2, 2, "Bob", "#222")

            original = PubSub.get_message
            failing = {"once": True}

            async def flaky_get_message(self, *args, **kwargs):
                if self.connection_pool is b._redis.connection_pool and failing["once"]:
                    failing["once"] = False
                    raise RedisConnectionError("connexion d'abonnement de B coupée (simulée)")
                return await original(self, *args, **kwargs)

            monkeypatch.setattr(PubSub, "get_message", flaky_get_message)
            await until(lambda: not b._subscribed)
            members[BOARD].discard(2)  # Bob retiré du tableau…
            await a.kick(BOARD, 2)  # …et le kick part pendant la coupure de B
            del members[BOARD + 1]  # second tableau supprimé
            await a.close_room(BOARD + 1)

            await until(lambda: b._subscribed, timeout=5)
            await until(lambda: bob_b.closed == 4403 and bob_b2.closed == 4404)
            assert revalidations and b.rooms == {}
            await a.publish(BOARD, {"type": "card.created", "revision": 99})
            await until(lambda: alice_a.of_type("card.created"))
            await asyncio.sleep(0.2)
            assert not bob_b.of_type("card.created")  # plus rien après le retrait

    run(scenario())


class StalledWS(FakeWS):
    """Client qui ne lit plus : chaque envoi reste bloqué (transport en pause)."""

    async def send_json(self, message):
        await asyncio.sleep(3600)


def test_stalled_client_delays_neither_its_board_nor_other_boards():
    async def scenario():
        async with cluster() as ((a, b), _):
            stalled, same_board, other_board = StalledWS(), FakeWS(), FakeWS()
            await b.join(BOARD, stalled, 1, "Lent", "#111")
            await b.join(BOARD, same_board, 2, "Bob", "#222")
            await b.join(BOARD + 1, other_board, 3, "Carol", "#333")
            await a.publish(BOARD, {"type": "card.moved", "revision": 1})
            await a.publish(BOARD + 1, {"type": "card.created", "revision": 1})
            await until(lambda: same_board.of_type("card.moved") and other_board.of_type("card.created"), timeout=1)

    run(scenario())


def test_slow_client_is_disconnected_when_its_queue_overflows_or_a_send_times_out(monkeypatch):
    """Même chemin en mémoire et avec Redis : file pleine → fermeture 4408 ; envoi bloqué au-delà
    du délai → connexion retirée et présence mise à jour."""
    monkeypatch.setattr(hub_module, "OUTBOX_MAX", 3)
    monkeypatch.setattr(hub_module, "SEND_TIMEOUT_SECONDS", 0.2)

    async def scenario():
        local = Hub()
        stalled, alice = StalledWS(), FakeWS()
        await local.join(BOARD, alice, 1, "Alice", "#111")
        await local.join(BOARD, stalled, 2, "Lent", "#222")
        # Envoi bloqué : au bout de 0,2 s la connexion est considérée morte et retirée
        await until(lambda: alice.users() == {"Alice": None}, timeout=2)
        assert [p.user_id for p in local.peers(BOARD)] == [1]

        overflow = StalledWS()
        monkeypatch.setattr(hub_module, "SEND_TIMEOUT_SECONDS", 60)
        await local.join(BOARD, overflow, 3, "Débordé", "#333")
        for revision in range(10):
            await local.publish(BOARD, {"type": "card.moved", "revision": revision})
            await asyncio.sleep(0.01)  # laisse écrire les clients qui lisent (file plafonnée à 3 ici)
        await until(lambda: overflow.closed == hub_module.SLOW_CLIENT_CODE, timeout=2)
        assert [p.user_id for p in local.peers(BOARD)] == [1]
        assert len(alice.of_type("card.moved")) == 10  # les autres ne sont pas touchés

    run(scenario())


def test_concurrent_leaves_with_out_of_order_presence_writes_leave_no_ghost(monkeypatch):
    """Deux départs simultanés dont la première écriture Redis est retardée : sans sérialisation,
    elle arrivait après la seconde et laissait un champ périmé (utilisateur fantôme)."""
    async def scenario():
        async with cluster() as ((a, b), _):
            alice, x, y = FakeWS(), FakeWS(), FakeWS()
            await a.join(BOARD, alice, 1, "Alice", "#111")
            await b.join(BOARD, x, 2, "X", "#222")
            await b.join(BOARD, y, 3, "Y", "#333")
            await until(lambda: alice.users() == {"Alice": None, "X": None, "Y": None})

            original_hset = b._redis.hset
            calls = []

            async def delayed_first_hset(*args, **kwargs):
                calls.append(args)
                if len(calls) == 1:
                    await asyncio.sleep(0.05)
                return await original_hset(*args, **kwargs)

            monkeypatch.setattr(b._redis, "hset", delayed_first_hset)
            await asyncio.gather(b.leave(BOARD, x), b.leave(BOARD, y))
            await until(lambda: alice.users() == {"Alice": None})
            await asyncio.sleep(0.5)  # plusieurs battements : l'état reste correct
            assert b.rooms == {} and alice.users() == {"Alice": None}
            assert list(await a._redis.hgetall(a._presence_key(BOARD))) == [a.instance_id]

    run(scenario())
