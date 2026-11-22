"""WebSocket d'un tableau : authentification, présence et diffusion des événements."""
import logging

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

import models
from access import membership_of
from auth import user_from_token
from database import SessionLocal
from hub import hub

logger = logging.getLogger("tandem")

# Délai laissé au client pour s'authentifier après l'ouverture du WebSocket
WS_AUTH_TIMEOUT = 5.0

router = APIRouter()


def _socket_identity(board_id: int, token: str | None) -> tuple[tuple[int, str, str] | None, bool]:
    """(identité, est membre ?) pour un jeton. Synchrone : exécuté hors de la boucle d'événements."""
    db = SessionLocal()
    try:
        user = user_from_token(db, token)
        if user is None:
            return None, False
        return (user.id, user.name, user.color), membership_of(db, board_id, user.id) is not None
    finally:
        db.close()


def _is_member(board_id: int, user_id: int) -> bool:
    db = SessionLocal()
    try:
        return membership_of(db, board_id, user_id) is not None
    finally:
        db.close()


def _current_members(connected: dict[int, set[int]]) -> dict[int, set[int] | None]:
    """Membres actuels des tableaux donnés parmi les utilisateurs connectés (None : tableau
    supprimé). Deux requêtes pour tous les tableaux. Synchrone : exécuté hors de la boucle."""
    db = SessionLocal()
    try:
        existing = set(db.scalars(select(models.Board.id).where(models.Board.id.in_(connected))))
        members: dict[int, set[int]] = {board_id: set() for board_id in existing}
        rows = db.execute(
            select(models.Membership.board_id, models.Membership.user_id)
            .where(models.Membership.board_id.in_(existing))
        )
        for board_id, user_id in rows:
            members[board_id].add(user_id)
        return {board_id: members.get(board_id) for board_id in connected}
    finally:
        db.close()


async def _revalidate_connections(connected: dict[int, set[int]]) -> dict[int, set[int] | None]:
    return await run_in_threadpool(_current_members, connected)


# Après une coupure de son abonnement Redis, le hub revérifie les droits de ses connexions
hub.revalidate = _revalidate_connections


async def _receive_token(ws: WebSocket) -> str | None:
    """Premier message attendu : {"type": "auth", "token": …}. Le jeton ne passe pas dans l'URL,
    qui finirait dans les journaux d'accès (l'API WebSocket des navigateurs ne permet pas
    d'envoyer d'en-tête Authorization)."""
    try:
        with anyio.fail_after(WS_AUTH_TIMEOUT):
            message = await ws.receive_json()
    except Exception:  # délai dépassé, JSON invalide, déconnexion…
        return None
    if not isinstance(message, dict) or message.get("type") != "auth":
        return None
    token = message.get("token")
    return token if isinstance(token, str) else None


async def _close(ws: WebSocket, code: int):
    try:
        await ws.close(code=code)
    except Exception:
        pass  # déjà fermée par le client


@router.websocket("/ws/boards/{board_id}")
async def board_socket(ws: WebSocket, board_id: int):
    """Canal temps réel d'un tableau. Le client s'authentifie par son premier message."""
    # On accepte avant de fermer : sinon le navigateur ne reçoit pas le code de fermeture
    await ws.accept()
    token = await _receive_token(ws)
    identity, is_member = await run_in_threadpool(_socket_identity, board_id, token)
    if identity is None:
        await _close(ws, 4401)
        return
    if not is_member:
        await _close(ws, 4403)
        return

    await hub.join(board_id, ws, *identity)
    # Second contrôle APRÈS l'entrée dans la room : un retrait validé entre le premier contrôle et
    # join() aurait raté cette connexion (hub.kick ne la voyait pas encore). Désormais, soit le
    # retrait est visible ici, soit kick, qui passe après son commit, trouvera la connexion.
    if not await run_in_threadpool(_is_member, board_id, identity[0]):
        await hub.leave(board_id, ws)
        await _close(ws, 4403)
        return
    try:
        while True:
            message = await ws.receive_json()
            kind = message.get("type") if isinstance(message, dict) else None
            if kind == "editing":
                card_id = message.get("card_id")
                await hub.set_editing(board_id, ws, card_id if isinstance(card_id, int) else None)
            elif kind == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("Connexion WebSocket interrompue", exc_info=True)
    finally:
        # Protégé de l'annulation : si la tâche est annulée (arrêt serveur, coupure brutale),
        # les autres membres doivent quand même voir cet utilisateur disparaître de la présence.
        with anyio.CancelScope(shield=True):
            await hub.leave(board_id, ws)
