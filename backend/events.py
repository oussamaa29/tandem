"""Révision du tableau, journal d'activité et diffusion des événements."""
from typing import Callable

from fastapi import BackgroundTasks
from sqlalchemy import update
from sqlalchemy.orm import Session

import models
from hub import hub
from serializers import activity_out


def emit(
    db: Session,
    background: BackgroundTasks,
    board: models.Board,
    actor: models.User,
    event_type: str,
    payload: Callable[[], dict],
    message: str | None = None,
) -> dict:
    """Incrémente la révision du tableau, journalise, commit, puis diffuse après la réponse.

    L'incrément est fait en SQL (revision = revision + 1 … RETURNING revision, une seule requête
    sur SQLite comme sur Postgres) pour rester correct si deux requêtes modifient le même tableau
    en même temps. En Postgres, la route appelante tient déjà le verrou de la ligne du tableau
    (lock_board, pris avant sa première écriture) jusqu'à ce commit : les écritures d'un tableau
    sont donc sérialisées et chacune lit la révision suivante. La révision permet aux clients de
    détecter un événement manqué et de se resynchroniser.

    Le contenu de l'événement est construit AVANT le commit, dans la transaction : après le
    commit, lire les objets forcerait un rechargement, qui échouerait si une requête concurrente
    venait de supprimer la ligne (écriture validée mais erreur 500 et aucune diffusion).
    Retourne ce contenu, que la route peut renvoyer tel quel.
    """
    revision = db.execute(
        update(models.Board)
        .where(models.Board.id == board.id)
        .values(revision=models.Board.revision + 1)
        .returning(models.Board.revision)
    ).scalar_one()
    activity = None
    if message:
        activity = models.Activity(board_id=board.id, user_id=actor.id, message=message)
        db.add(activity)
    db.flush()

    data = payload()
    event = {"type": event_type, "revision": revision, "actor_id": actor.id, "payload": data}
    if activity:
        event["activity"] = activity_out(activity)
    db.commit()
    background.add_task(hub.publish, board.id, event)
    return data
