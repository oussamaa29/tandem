"""Diffusion temps réel : une « room » WebSocket par tableau.

Les modifications passent par l'API REST (validées et transactionnelles), puis l'événement
résultant est diffusé ici à tous les membres connectés au tableau. Le WebSocket ne sert, dans
l'autre sens, qu'à des signaux éphémères : présence et « est en train de modifier ».

Deux modes :

- sans REDIS_URL (développement local) : tout est en mémoire, valable pour un seul processus ;
- avec REDIS_URL : plusieurs instances derrière un répartiteur de charge. Chaque instance garde
  ses propres connexions WebSocket, mais tout ce qui doit atteindre les membres connectés
  AILLEURS transite par Redis :
    * événements, retrait d'un membre (kick) et suppression du tableau (close) sont publiés sur
      le canal `tandem:board:<id>` ; chaque instance est abonnée à `tandem:board:*` et les
      remet à SES connexions. Les connexions locales ne reçoivent rien directement (pas de
      double réception), sauf si l'abonnement est coupé (voir plus bas) ;
    * la présence est globale : chaque instance (identifiant aléatoire) écrit la liste de ses
      utilisateurs dans le hash `tandem:presence:<id>` (un champ par instance), puis publie
      une notification ; chaque instance relit alors le hash et envoie la liste fusionnée
      (dédoublonnée par utilisateur) à ses connexions. Une clé de vie par instance, avec TTL
      et rafraîchie par un battement régulier, permet d'écarter les utilisateurs d'une
      instance arrêtée brutalement.

Redis indisponible : on journalise, les messages sont remis aux seules connexions de cette
instance, et l'abonnement se reconnecte avec un délai exponentiel. Les événements perdus
pendant la coupure sont rattrapés par les clients, qui se resynchronisent en voyant un trou
dans les révisions du tableau.
"""

import asyncio
import json
import logging
import random
import uuid
from dataclasses import dataclass, field

import redis.asyncio as aioredis
from fastapi import WebSocket
from redis.exceptions import RedisError

from config import settings

logger = logging.getLogger("tandem.hub")

# Battement de vie de l'instance ; au-delà du TTL sans battement, ses utilisateurs disparaissent
HEARTBEAT_SECONDS = 5.0
INSTANCE_TTL_SECONDS = 15.0
# Reconnexion de l'abonnement Redis : délai doublé à chaque échec, plafonné
RETRY_MIN_SECONDS = 0.5
RETRY_MAX_SECONDS = 10.0

REDIS_ERRORS = (RedisError, OSError, asyncio.TimeoutError)


@dataclass(eq=False)
class Peer:
    ws: WebSocket
    user_id: int
    name: str
    color: str
    editing_card_id: int | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: dict) -> bool:
        try:
            # Un verrou par connexion : deux diffusions simultanées ne s'entremêlent pas
            async with self.lock:
                await self.ws.send_json(message)
            return True
        except Exception:
            return False


class Hub:
    def __init__(
        self,
        redis_url: str | None = None,
        *,
        redis_client=None,
        instance_id: str | None = None,
        prefix: str = "tandem",
        heartbeat: float = HEARTBEAT_SECONDS,
        instance_ttl: float = INSTANCE_TTL_SECONDS,
        retry_min: float = RETRY_MIN_SECONDS,
        retry_max: float = RETRY_MAX_SECONDS,
    ):
        self.rooms: dict[int, dict[WebSocket, Peer]] = {}
        self.redis_url = redis_url
        # Client fourni de l'extérieur (tests) : ni créé ni fermé par le hub
        self._external_client = redis_client
        self.instance_id = instance_id or uuid.uuid4().hex[:12]
        self.prefix = prefix
        self.heartbeat = heartbeat
        self.instance_ttl = instance_ttl
        self.retry_min = retry_min
        self.retry_max = retry_max
        self._redis = None
        self._tasks: list[asyncio.Task] = []
        # Vrai quand l'abonnement Redis est actif : les messages locaux arrivent alors par lui
        self._subscribed = False

    # ---------------- Cycle de vie (lifespan FastAPI) ----------------

    async def start(self):
        if self._redis is not None or (self._external_client is None and not self.redis_url):
            return
        self._redis = self._external_client or aioredis.from_url(
            self.redis_url, decode_responses=True,
            socket_connect_timeout=2, socket_timeout=5, health_check_interval=15,
        )
        try:
            await self._touch_instance()
        except REDIS_ERRORS as exc:
            logger.warning("Redis injoignable au démarrage (%s) : diffusion limitée à cette instance", exc)
        self._tasks = [asyncio.create_task(self._subscriber()), asyncio.create_task(self._heartbeat_loop())]

    async def stop(self):
        if self._redis is None:
            return
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._subscribed = False
        # Arrêt propre : nos utilisateurs disparaissent tout de suite de la présence des autres
        try:
            async with asyncio.timeout(2):
                for board_id in list(self.rooms):
                    await self._redis.hdel(self._presence_key(board_id), self.instance_id)
                    await self._redis.publish(self._channel(board_id), json.dumps({"kind": "presence"}))
                await self._redis.delete(self._instance_key)
        except REDIS_ERRORS:
            pass
        if self._external_client is None:
            await self._redis.aclose()
        self._redis = None

    # ---------------- Interface utilisée par main.py ----------------

    def peers(self, board_id: int) -> list[Peer]:
        """Connexions de CETTE instance."""
        return list(self.rooms.get(board_id, {}).values())

    def presence(self, board_id: int) -> list[dict]:
        """Présence locale. Un utilisateur avec plusieurs onglets n'apparaît qu'une fois."""
        users: dict[int, dict] = {}
        for p in self.peers(board_id):
            entry = users.setdefault(
                p.user_id, {"id": p.user_id, "name": p.name, "color": p.color, "editing_card_id": None}
            )
            if p.editing_card_id is not None:
                entry["editing_card_id"] = p.editing_card_id
        return list(users.values())

    async def join(self, board_id: int, ws: WebSocket, user_id: int, name: str, color: str) -> Peer:
        peer = Peer(ws=ws, user_id=user_id, name=name, color=color)
        self.rooms.setdefault(board_id, {})[ws] = peer
        await self._presence_changed(board_id)
        return peer

    async def leave(self, board_id: int, ws: WebSocket):
        room = self.rooms.get(board_id)
        if room is None or ws not in room:
            return
        del room[ws]
        if not room:
            del self.rooms[board_id]
        await self._presence_changed(board_id)

    async def set_editing(self, board_id: int, ws: WebSocket, card_id: int | None):
        peer = self.rooms.get(board_id, {}).get(ws)
        if peer is None or peer.editing_card_id == card_id:
            return
        peer.editing_card_id = card_id
        await self._presence_changed(board_id)

    async def publish(self, board_id: int, event: dict):
        await self._fanout(board_id, {"kind": "event", "event": event})

    async def kick(self, board_id: int, user_id: int, code: int = 4403):
        """Ferme les connexions d'un utilisateur retiré du tableau, sur toutes les instances."""
        await self._fanout(board_id, {"kind": "kick", "user_id": user_id, "code": code})

    async def close_room(self, board_id: int, code: int = 4404):
        """Tableau supprimé : ferme toutes ses connexions, sur toutes les instances."""
        await self._fanout(board_id, {"kind": "close", "code": code})

    # ---------------- Acheminement ----------------

    def _channel(self, board_id: int) -> str:
        return f"{self.prefix}:board:{board_id}"

    def _presence_key(self, board_id: int) -> str:
        return f"{self.prefix}:presence:{board_id}"

    def _instance_key_for(self, instance_id: str) -> str:
        return f"{self.prefix}:instance:{instance_id}"

    @property
    def _instance_key(self) -> str:
        return self._instance_key_for(self.instance_id)

    @property
    def _ttl_ms(self) -> int:
        return int(self.instance_ttl * 1000)

    async def _fanout(self, board_id: int, envelope: dict):
        """Remet un message à toutes les instances via Redis ; à défaut, à cette instance seule."""
        if self._redis is None:
            await self._handle(board_id, envelope)
            return
        try:
            await self._redis.publish(self._channel(board_id), json.dumps(envelope))
        except REDIS_ERRORS as exc:
            logger.warning("Publication Redis impossible (%s) : remise aux connexions locales uniquement", exc)
            await self._handle(board_id, envelope)
            return
        if not self._subscribed:
            # Abonnement coupé : le message ne nous reviendra pas, on le remet nous-mêmes
            await self._handle(board_id, envelope)

    async def _handle(self, board_id: int, envelope: dict):
        kind = envelope.get("kind")
        if kind == "event":
            await self._send_all(board_id, envelope["event"])
        elif kind == "presence":
            await self._send_presence(board_id)
        elif kind == "kick":
            for peer in self.peers(board_id):
                if peer.user_id == envelope["user_id"]:
                    await self._close(peer, envelope["code"])
                    await self.leave(board_id, peer.ws)
        elif kind == "close":
            for peer in self.peers(board_id):
                await self._close(peer, envelope["code"])
            self.rooms.pop(board_id, None)
            if self._redis is not None:
                try:
                    await self._redis.hdel(self._presence_key(board_id), self.instance_id)
                except REDIS_ERRORS:
                    pass

    # ---------------- Présence ----------------

    async def _presence_changed(self, board_id: int):
        if self._redis is not None:
            try:
                await self._write_presence(board_id)
            except REDIS_ERRORS as exc:
                logger.warning("Écriture de la présence dans Redis impossible (%s)", exc)
        await self._fanout(board_id, {"kind": "presence"})

    async def _write_presence(self, board_id: int):
        key = self._presence_key(board_id)
        users = self.presence(board_id)
        if users:
            await self._redis.hset(key, self.instance_id, json.dumps(users))
            await self._redis.pexpire(key, self._ttl_ms)
        else:
            await self._redis.hdel(key, self.instance_id)

    async def _alive(self, instance_ids: list[str]) -> set[str]:
        if not instance_ids:
            return set()
        values = await self._redis.mget([self._instance_key_for(i) for i in instance_ids])
        return {i for i, v in zip(instance_ids, values) if v is not None}

    async def merged_presence(self, board_id: int) -> list[dict]:
        """Présence de toutes les instances (instances mortes exclues), dédoublonnée par utilisateur.
        Sans Redis, ou s'il est injoignable : présence locale."""
        if self._redis is None:
            return self.presence(board_id)
        try:
            raw = await self._redis.hgetall(self._presence_key(board_id))
            alive = await self._alive([i for i in raw if i != self.instance_id])
        except REDIS_ERRORS:
            return self.presence(board_id)
        # Notre propre part vient de la mémoire, toujours plus fraîche que Redis
        lists = {i: json.loads(raw[i]) for i in alive}
        lists[self.instance_id] = self.presence(board_id)
        users: dict[int, dict] = {}
        for instance in sorted(lists):
            for u in lists[instance]:
                entry = users.setdefault(u["id"], {**u, "editing_card_id": None})
                if u.get("editing_card_id") is not None:
                    entry["editing_card_id"] = u["editing_card_id"]
        return list(users.values())

    async def _send_presence(self, board_id: int):
        if not self.peers(board_id):
            return
        await self._send_all(board_id, {"type": "presence", "users": await self.merged_presence(board_id)})

    async def _sweep(self, board_id: int) -> bool:
        """Retire de la présence les instances dont la clé de vie a expiré (arrêt brutal)."""
        others = [i for i in await self._redis.hkeys(self._presence_key(board_id)) if i != self.instance_id]
        dead = set(others) - await self._alive(others)
        if dead:
            await self._redis.hdel(self._presence_key(board_id), *dead)
            logger.info("Présence : instance(s) %s disparue(s) du tableau %s", ", ".join(sorted(dead)), board_id)
        return bool(dead)

    async def _touch_instance(self):
        await self._redis.set(self._instance_key, "1", px=self._ttl_ms)

    # ---------------- Tâches de fond (mode Redis) ----------------

    async def _heartbeat_loop(self):
        while True:
            await asyncio.sleep(self.heartbeat)
            try:
                await self._touch_instance()
                for board_id in list(self.rooms):
                    await self._write_presence(board_id)
                    if await self._sweep(board_id):
                        await self._fanout(board_id, {"kind": "presence"})
            except REDIS_ERRORS as exc:
                logger.debug("Battement Redis impossible (%s)", exc)

    async def _subscriber(self):
        delay = self.retry_min
        while True:
            pubsub = self._redis.pubsub()
            try:
                await pubsub.psubscribe(f"{self.prefix}:board:*")
                self._subscribed = True
                delay = self.retry_min
                logger.info("Abonné à Redis (instance %s)", self.instance_id)
                # Après une coupure : republie notre présence, que les autres ont pu perdre
                await self._touch_instance()
                for board_id in list(self.rooms):
                    await self._presence_changed(board_id)
                while True:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if message and message["type"] == "pmessage":
                        await self._dispatch(message["channel"], message["data"])
            except REDIS_ERRORS as exc:
                logger.warning("Abonnement Redis interrompu (%s) : diffusion limitée à cette instance, "
                               "reconnexion dans %.1f s", exc, delay)
            finally:
                self._subscribed = False
                try:
                    await pubsub.aclose()
                except Exception:
                    pass
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))
            delay = min(delay * 2, self.retry_max)

    async def _dispatch(self, channel, data):
        try:
            if isinstance(channel, bytes):
                channel, data = channel.decode(), data.decode()
            board_id = int(channel.rsplit(":", 1)[1])
            await self._handle(board_id, json.loads(data))
        except REDIS_ERRORS:
            raise
        except Exception:
            # Un message invalide ne doit pas arrêter l'abonnement
            logger.exception("Message Redis ignoré sur %s", channel)

    # ---------------- Envoi aux connexions locales ----------------

    async def _send_all(self, board_id: int, message: dict):
        peers = self.peers(board_id)
        if not peers:
            return
        results = await asyncio.gather(*(p.send(message) for p in peers))
        dead = [p for p, ok in zip(peers, results) if not ok]
        for p in dead:
            room = self.rooms.get(board_id)
            if room is not None:
                room.pop(p.ws, None)
                if not room:
                    del self.rooms[board_id]
        if dead:
            logger.info("%d connexion(s) morte(s) retirée(s) du tableau %s", len(dead), board_id)
            await self._presence_changed(board_id)

    @staticmethod
    async def _close(peer: Peer, code: int):
        try:
            await peer.ws.close(code=code)
        except Exception:
            pass


hub = Hub(settings.REDIS_URL)
