"""Diffusion temps réel : une « room » WebSocket par tableau.

Les modifications passent par l'API REST (validées et transactionnelles), puis l'événement
résultant est diffusé ici à tous les membres connectés au tableau. Le WebSocket ne sert, dans
l'autre sens, qu'à des signaux éphémères : présence et « est en train de modifier ».

État en mémoire, donc valable pour un seul processus. Pour plusieurs instances, il suffit de
faire transiter `publish` par un pub/sub (Redis, Postgres LISTEN/NOTIFY) — l'interface ne change pas.
"""

import asyncio
import logging
from dataclasses import dataclass, field

from fastapi import WebSocket

logger = logging.getLogger("tandem.hub")


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
    def __init__(self):
        self.rooms: dict[int, dict[WebSocket, Peer]] = {}

    def peers(self, board_id: int) -> list[Peer]:
        return list(self.rooms.get(board_id, {}).values())

    def presence(self, board_id: int) -> list[dict]:
        # Un utilisateur avec plusieurs onglets n'apparaît qu'une fois
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
        await self._broadcast_presence(board_id)
        return peer

    async def leave(self, board_id: int, ws: WebSocket):
        room = self.rooms.get(board_id)
        if room is None or ws not in room:
            return
        del room[ws]
        if not room:
            del self.rooms[board_id]
        await self._broadcast_presence(board_id)

    async def set_editing(self, board_id: int, ws: WebSocket, card_id: int | None):
        peer = self.rooms.get(board_id, {}).get(ws)
        if peer is None or peer.editing_card_id == card_id:
            return
        peer.editing_card_id = card_id
        await self._broadcast_presence(board_id)

    async def publish(self, board_id: int, event: dict):
        await self._send_all(board_id, event)

    async def kick(self, board_id: int, user_id: int, code: int = 4403):
        """Ferme les connexions d'un utilisateur retiré du tableau."""
        for peer in self.peers(board_id):
            if peer.user_id == user_id:
                await self._close(peer, code)
                await self.leave(board_id, peer.ws)

    async def close_room(self, board_id: int, code: int = 4404):
        for peer in self.peers(board_id):
            await self._close(peer, code)
        self.rooms.pop(board_id, None)

    async def _broadcast_presence(self, board_id: int):
        await self._send_all(board_id, {"type": "presence", "users": self.presence(board_id)})

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
        if dead:
            logger.info("%d connexion(s) morte(s) retirée(s) du tableau %s", len(dead), board_id)
            await self._broadcast_presence(board_id)

    @staticmethod
    async def _close(peer: Peer, code: int):
        try:
            await peer.ws.close(code=code)
        except Exception:
            pass


hub = Hub()
