"""Cartes : création, édition avec verrou optimiste, déplacement, suppression."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, update
from sqlalchemy.orm import Session

import models
import schemas
from access import card_for, column_for, lock_board, membership_of
from auth import get_current_user
from database import get_db
from events import emit
from positions import evenly_spaced, position_at
from serializers import card_out

router = APIRouter()


@router.post("/api/columns/{column_id}/cards", status_code=201)
def create_card(
    column_id: int, payload: schemas.CardIn, background: BackgroundTasks,
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db),
):
    col, board = column_for(db, user, column_id)
    lock_board(db, board.id)
    last = db.query(func.max(models.Card.position)).filter_by(column_id=col.id).scalar()
    card = models.Card(
        board_id=board.id, column_id=col.id, title=payload.title,
        position=(last or 0) + 1024, created_by_id=user.id,
    )
    db.add(card)
    db.flush()
    return emit(db, background, board, user, "card.created", lambda: card_out(card),
                f"a ajouté « {card.title} » dans {col.name}")


@router.patch("/api/cards/{card_id}")
def update_card(
    card_id: int, payload: schemas.CardUpdate, background: BackgroundTasks,
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db),
):
    """Édition du contenu avec verrou optimiste.

    Le client envoie la `version` qu'il a affichée. Si quelqu'un a modifié la carte entre-temps,
    on refuse (409) en renvoyant la version à jour plutôt que d'écraser silencieusement son travail.
    """
    card, board = card_for(db, user, card_id)
    lock_board(db, board.id)
    if payload.version != card.version:
        raise HTTPException(409, {"message": "La carte a été modifiée entre-temps", "card": card_out(card)})

    changes = payload.model_dump(exclude_unset=True, exclude={"version"})
    if "title" in changes and changes["title"] is None:
        raise HTTPException(422, "Le titre ne peut pas être vide")
    if "description" in changes and changes["description"] is None:
        changes["description"] = ""  # « pas de description » = chaîne vide (colonne non nulle)
    if changes.get("assignee_id") is not None and not membership_of(db, board.id, changes["assignee_id"]):
        raise HTTPException(400, "On ne peut assigner qu'un membre du tableau")
    changes = {k: v for k, v in changes.items() if getattr(card, k) != v}
    if not changes:
        return card_out(card)

    # Vérification et écriture en une seule requête (UPDATE … WHERE version = :v) : si une autre
    # requête a enregistré entre notre lecture et notre écriture, aucune ligne n'est modifiée et
    # on répond 409 comme ci-dessus, au lieu d'écraser silencieusement son travail.
    result = db.execute(
        update(models.Card)
        .where(models.Card.id == card.id, models.Card.version == payload.version)
        .values(**changes, version=models.Card.version + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        current = db.get(models.Card, card_id)
        if current is None:
            raise HTTPException(404, "Carte introuvable")
        raise HTTPException(409, {"message": "La carte a été modifiée entre-temps", "card": card_out(current)})
    db.refresh(card)

    if "assignee_id" in changes:
        assignee = db.get(models.User, card.assignee_id) if card.assignee_id else None
        message = f"a assigné « {card.title} » à {assignee.name}" if assignee else f"a désassigné « {card.title} »"
    elif "title" in changes:
        message = f"a renommé une carte en « {card.title} »"
    else:
        message = f"a modifié la description de « {card.title} »"
    return emit(db, background, board, user, "card.updated", lambda: card_out(card), message)


@router.post("/api/cards/{card_id}/move")
def move_card(
    card_id: int, payload: schemas.CardMoveIn, background: BackgroundTasks,
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db),
):
    """Déplace une carte à `index` dans la colonne cible. Le serveur calcule la position :
    le client ne fournit qu'une intention, jamais une valeur de tri brute."""
    card, board = card_for(db, user, card_id)
    lock_board(db, board.id)
    target = db.get(models.BoardColumn, payload.column_id)
    if not target or target.board_id != board.id:
        raise HTTPException(404, "Colonne introuvable")
    source_column_id = card.column_id

    # Deux déplacements simultanés peuvent calculer la même position : le départage par id garde
    # un ordre déterministe, et la prochaine insertion entre ces deux cartes les rééquilibre
    siblings = (
        db.query(models.Card)
        .filter(models.Card.column_id == target.id, models.Card.id != card.id)
        .order_by(models.Card.position, models.Card.id)
        .all()
    )
    position = position_at([c.position for c in siblings], payload.index)
    rebalanced = []
    card.column_id = target.id
    if position is None:
        ordered = siblings[:]
        ordered.insert(min(payload.index, len(ordered)), card)
        for c, pos in zip(ordered, evenly_spaced(len(ordered))):
            c.position = pos
        rebalanced = [{"id": c.id, "position": c.position} for c in ordered]
    else:
        card.position = position
    db.flush()

    message = f"a déplacé « {card.title} » vers {target.name}" if source_column_id != target.id else None
    data = emit(db, background, board, user, "card.moved",
                lambda: {"card": card_out(card), "rebalanced": rebalanced}, message)
    return data["card"]


@router.delete("/api/cards/{card_id}", status_code=204)
def delete_card(
    card_id: int, background: BackgroundTasks,
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db),
):
    card, board = card_for(db, user, card_id)
    lock_board(db, board.id)
    title = card.title
    db.delete(card)
    db.flush()
    emit(db, background, board, user, "card.deleted", lambda: {"id": card_id}, f"a supprimé « {title} »")
    return None
