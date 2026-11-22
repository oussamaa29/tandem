"""Contrôle d'accès aux tableaux, colonnes et cartes, et verrou d'écriture d'un tableau."""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

import models


def membership_of(db: Session, board_id: int, user_id: int) -> models.Membership | None:
    return db.query(models.Membership).filter_by(board_id=board_id, user_id=user_id).first()


def board_for(db: Session, user: models.User, board_id: int, owner_only: bool = False) -> models.Board:
    board = db.get(models.Board, board_id)
    membership = board and membership_of(db, board.id, user.id)
    # 404 et non 403 : on ne révèle pas l'existence des tableaux des autres
    if not board or not membership:
        raise HTTPException(404, "Tableau introuvable")
    if owner_only and membership.role != models.OWNER:
        raise HTTPException(403, "Action réservée au propriétaire du tableau")
    return board


def column_for(db: Session, user: models.User, column_id: int) -> tuple[models.BoardColumn, models.Board]:
    col = db.get(models.BoardColumn, column_id)
    if not col or not membership_of(db, col.board_id, user.id):
        raise HTTPException(404, "Colonne introuvable")
    return col, db.get(models.Board, col.board_id)


def card_for(db: Session, user: models.User, card_id: int) -> tuple[models.Card, models.Board]:
    card = db.get(models.Card, card_id)
    if not card or not membership_of(db, card.board_id, user.id):
        raise HTTPException(404, "Carte introuvable")
    return card, db.get(models.Board, card.board_id)


def lock_board(db: Session, board_id: int) -> None:
    """Verrouille la ligne du tableau (SELECT … FOR NO KEY UPDATE) : à appeler par chaque route
    d'écriture après le contrôle d'accès, AVANT sa première écriture.

    En Postgres, les écritures d'un même tableau prennent ainsi toutes leurs verrous dans le même
    ordre (tableau, puis cartes / colonnes / membres). Sans cela, une route verrouillait d'abord sa
    carte puis le tableau (dans emit), tandis que la suppression du tableau verrouillait le tableau
    puis, par ON DELETE CASCADE, les cartes : interblocage, et l'une des deux finissait en 500.
    NO KEY UPDATE (le verrou que prend déjà UPDATE boards SET revision) exclut les autres écritures
    et la suppression du tableau, sans bloquer les simples vérifications de clé étrangère.
    Si le tableau a été supprimé pendant l'attente, 404. Sans effet en SQLite, qui sérialise de
    toute façon les écritures sur la base entière."""
    locked = db.execute(
        select(models.Board.id).where(models.Board.id == board_id).with_for_update(key_share=True)
    ).first()
    if locked is None:
        raise HTTPException(404, "Tableau introuvable")
