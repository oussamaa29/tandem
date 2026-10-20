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
            assert len(alice_a.of_type("card.created")) == 1
            await a.join(BOARD, bob_a, 2, "Bob", "#222")
            assert set(bob_a.users()) >= {"Alice", "Bob"}
            await a.kick(BOARD, 2)
            assert bob_a.closed == 4403
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
        assert local._tasks == [] and len(ws.of_type("card.created")) == 1
        assert ws.users() == {"Alice": None}
        await local.stop()

    run(scenario())
