"""Diffusion temps réel : une « room » WebSocket par tableau.

Les modifications passent par l'API REST (validées et transactionnelles), puis l'événement
résultant est diffusé ici à tous les membres connectés au tableau. Le WebSocket ne sert, dans
l'autre sens, qu'à des signaux éphémères : présence et « est en train de modifier ».

Chaque connexion a sa file d'envoi et sa tâche d'écriture : diffuser ne fait que mettre en file,
sans jamais attendre un client. Un client qui ne lit plus ne retarde donc ni les autres
connexions ni les autres tableaux ; si sa file déborde, ou si un envoi dépasse le délai, sa
connexion est fermée (4408) et le client se reconnecte puis se resynchronise.

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

Abonnement Redis coupé (Redis arrêté, ou seulement la connexion de cette instance) : on
journalise, les messages publiés ici sont remis directement aux connexions locales, et
l'abonnement se reconnecte avec un délai exponentiel. Ce qui a été publié ailleurs pendant la
coupure est perdu pour cette instance :
    * les événements sont rattrapés par les clients, qui se resynchronisent en voyant un trou
      dans les révisions du tableau ;
    * les retraits de membre et suppressions de tableau ne le seraient pas : à chaque
      réabonnement, les connexions locales sont donc revalidées auprès de la base (callback
      `revalidate`, fourni par main.py) avant de recevoir quoi que ce soit.
"""

import asyncio
import json
import logging
import random
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
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
# Client trop lent : file d'envoi pleine ou envoi bloqué trop longtemps → connexion fermée
OUTBOX_MAX = 256
SEND_TIMEOUT_SECONDS = 10.0
CLOSE_TIMEOUT_SECONDS = 2.0
SLOW_CLIENT_CODE = 4408

REDIS_ERRORS = (RedisError, OSError, asyncio.TimeoutError)

# {tableau: utilisateurs connectés ici} → {tableau: membres actuels, ou None si supprimé}
Revalidator = Callable[[dict[int, set[int]]], Awaitable[dict[int, set[int] | None]]]


@dataclass(eq=False)
class Peer:
    ws: WebSocket
    user_id: int
    name: str
    color: str
    editing_card_id: int | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    outbox: deque = field(default_factory=deque)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    # Fermeture demandée : effectuée par la tâche d'écriture APRÈS les messages déjà en file
    close_code: int | None = None
    # Appelé quand un envoi échoue (connexion morte ou client bloqué)
    on_dead: Callable[["Peer"], Awaitable[None]] | None = None
    writer: asyncio.Task | None = None

    def start(self):
        self.writer = asyncio.create_task(self._write_loop())

    def send(self, message: dict) -> bool:
        """Met en file sans attendre le client. False si la connexion est fermée ou va l'être."""
        if self.close_code is not None:
            return False
        if len(self.outbox) >= OUTBOX_MAX:
            logger.warning("File d'envoi pleine pour l'utilisateur %s : connexion fermée", self.user_id)
            self.abort(SLOW_CLIENT_CODE)
            return False
        self.outbox.append(message)
        self.wake.set()
        return True

    def close(self, code: int):
        if self.close_code is None:
            self.close_code = code
            self.wake.set()

    def abort(self, code: int):
        """Client trop lent : abandonne la file et l'envoi en cours, ferme sans attendre."""
        self.outbox.clear()
        self.close_code = code
        if self.writer is not None:
            self.writer.cancel()
        self.writer = asyncio.create_task(self._close_ws())

    async def _close_ws(self):
        try:
            await asyncio.wait_for(self.ws.close(code=self.close_code or SLOW_CLIENT_CODE), CLOSE_TIMEOUT_SECONDS)
        except Exception:
            pass  # déjà fermée par le client

    def discard(self):
        """Le client est parti : plus rien à lui envoyer (une fermeture demandée va à son terme)."""
        if self.close_code is None and self.writer is not None:
            self.writer.cancel()

    async def _write_loop(self):
        failed = False
        try:
            while True:
                while self.outbox:
                    message = self.outbox.popleft()
                    # Un seul écrivain par connexion, et le verrou en plus : jamais d'envois entremêlés
                    async with self.lock:
                        await asyncio.wait_for(self.ws.send_json(message), SEND_TIMEOUT_SECONDS)
                if self.close_code is not None:
                    break
                self.wake.clear()
                await self.wake.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            failed = True  # connexion morte, ou client qui ne lit plus
        await self._close_ws()
        if failed and self.on_dead is not None:
            await self.on_dead(self)


class Hub:
    def __init__(
        self,
        redis_url: str | None = None,
        *,
        redis_client=None,
        revalidate: Revalidator | None = None,
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
        self.revalidate = revalidate
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
        # Écritures de présence sérialisées par tableau, et tableaux où notre champ existe
        self._presence_locks: dict[int, asyncio.Lock] = {}
        self._presence_written: set[int] = set()

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
                for board_id in set(self.rooms) | self._presence_written:
                    await self._redis.hdel(self._presence_key(board_id), self.instance_id)
                    await self._redis.publish(self._channel(board_id), json.dumps({"kind": "presence"}))
                await self._redis.delete(self._instance_key)
        except REDIS_ERRORS:
            pass
        self._presence_written.clear()
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
        async def on_dead(peer: Peer):
            await self._drop(board_id, peer)

        peer = Peer(ws=ws, user_id=user_id, name=name, color=color, on_dead=on_dead)
        peer.start()
        self.rooms.setdefault(board_id, {})[ws] = peer
        await self._presence_changed(board_id)
        return peer

    async def leave(self, board_id: int, ws: WebSocket):
        room = self.rooms.get(board_id)
        if room is None or ws not in room:
            return
        room.pop(ws).discard()
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
        # Abonnement coupé : le message est remis ici directement, et marqué pour que notre
        # abonnement, s'il revient entre-temps, ne le remette pas une seconde fois
        local = not self._subscribed
        envelope = {**envelope, "origin": self.instance_id, "local": local}
        try:
            await self._redis.publish(self._channel(board_id), json.dumps(envelope))
        except REDIS_ERRORS as exc:
            logger.warning("Publication Redis impossible (%s) : remise aux connexions locales uniquement", exc)
            local = True
        if local:
            await self._handle(board_id, envelope)

    async def _handle(self, board_id: int, envelope: dict):
        """Traite un message pour les connexions locales. Ne fait jamais attendre un client."""
        kind = envelope.get("kind")
        if kind == "event":
            await self._send_all(board_id, envelope["event"])
        elif kind == "presence":
            await self._send_presence(board_id)
        elif kind == "kick":
            await self._kick_local(board_id, {envelope["user_id"]}, envelope["code"])
        elif kind == "close":
            await self._close_local(board_id, envelope["code"])

    async def _kick_local(self, board_id: int, user_ids: set[int], code: int):
        for peer in self.peers(board_id):
            if peer.user_id in user_ids:
                peer.close(code)
                await self.leave(board_id, peer.ws)

    async def _close_local(self, board_id: int, code: int):
        for peer in self.peers(board_id):
            peer.close(code)
        self.rooms.pop(board_id, None)
        if self._redis is not None:
            try:
                await self._write_presence(board_id)
            except REDIS_ERRORS:
                pass

    async def _revalidate_peers(self):
        """Après une coupure de l'abonnement : ferme les connexions dont l'utilisateur n'est plus
        membre (4403) et celles des tableaux supprimés (4404), retraits manqués pendant la coupure."""
        if self.revalidate is None or not self.rooms:
            return
        connected = {board_id: {p.user_id for p in self.peers(board_id)} for board_id in list(self.rooms)}
        try:
            members = await self.revalidate(connected)
        except Exception:
            logger.exception("Revalidation des connexions impossible")
            return
        for board_id, allowed in members.items():
            if allowed is None:
                await self._close_local(board_id, 4404)
            else:
                await self._kick_local(board_id, connected[board_id] - allowed, 4403)

    # ---------------- Présence ----------------

    async def _presence_changed(self, board_id: int):
        if self._redis is not None:
            try:
                await self._write_presence(board_id)
            except REDIS_ERRORS as exc:
                logger.warning("Écriture de la présence dans Redis impossible (%s)", exc)
        await self._fanout(board_id, {"kind": "presence"})

    async def _write_presence(self, board_id: int):
        # Sérialisé par tableau, instantané pris SOUS le verrou : deux départs simultanés (ou un
        # départ et le battement) ne peuvent pas appliquer leurs écritures dans le désordre et
        # laisser un champ périmé avec des utilisateurs partis
        async with self._presence_locks.setdefault(board_id, asyncio.Lock()):
            key = self._presence_key(board_id)
            users = self.presence(board_id)
            if users:
                await self._redis.hset(key, self.instance_id, json.dumps(users))
                await self._redis.pexpire(key, self._ttl_ms)
                self._presence_written.add(board_id)
            else:
                await self._redis.hdel(key, self.instance_id)
                self._presence_written.discard(board_id)

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
                # Tableaux encore occupés ici (champ rafraîchi), et tableaux quittés dont le champ
                # existe encore (supprimé : filet de sécurité si sa suppression a échoué)
                for board_id in set(self.rooms) | self._presence_written:
                    await self._write_presence(board_id)
                    if board_id in self.rooms and await self._sweep(board_id):
                        await self._fanout(board_id, {"kind": "presence"})
                for board_id, lock in list(self._presence_locks.items()):
                    if board_id not in self.rooms and board_id not in self._presence_written and not lock.locked():
                        del self._presence_locks[board_id]
            except REDIS_ERRORS as exc:
                logger.debug("Battement Redis impossible (%s)", exc)

    async def _subscriber(self):
        delay = self.retry_min
        while True:
            pubsub = self._redis.pubsub()
            try:
                await pubsub.psubscribe(f"{self.prefix}:board:*")
                await self._touch_instance()
                # Avant de remettre quoi que ce soit : rattrape les retraits et suppressions publiés
                # pendant une coupure (les messages arrivés depuis attendent dans l'abonnement)
                await self._revalidate_peers()
                self._subscribed = True
                delay = self.retry_min
                logger.info("Abonné à Redis (instance %s)", self.instance_id)
                # Après une coupure : republie notre présence, que les autres ont pu perdre
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
            envelope = json.loads(data)
            if envelope.get("local") and envelope.get("origin") == self.instance_id:
                return  # déjà remis directement par nous (abonnement coupé à la publication)
            await self._handle(board_id, envelope)
        except REDIS_ERRORS:
            raise
        except Exception:
            # Un message invalide ne doit pas arrêter l'abonnement
            logger.exception("Message Redis ignoré sur %s", channel)

    # ---------------- Envoi aux connexions locales ----------------

    async def _send_all(self, board_id: int, message: dict):
        # Mise en file seulement : aucun client ne peut ralentir la diffusion
        dead = [p for p in self.peers(board_id) if not p.send(message)]
        for p in dead:
            await self._drop(board_id, p, notify=False)
        if dead:
            logger.info("%d connexion(s) morte(s) retirée(s) du tableau %s", len(dead), board_id)
            await self._presence_changed(board_id)

    async def _drop(self, board_id: int, peer: Peer, notify: bool = True):
        """Retire une connexion morte ou trop lente (si elle est encore dans la room)."""
        room = self.rooms.get(board_id)
        if room is None or room.get(peer.ws) is not peer:
            return
        del room[peer.ws]
        if not room:
            del self.rooms[board_id]
        if notify:
            await self._presence_changed(board_id)


hub = Hub(settings.REDIS_URL)
