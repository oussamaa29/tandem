"""Tableaux, journal d'activité et membres."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

import models
import schemas
from access import board_for, lock_board, membership_of
from auth import get_current_user
from database import get_db, is_unique_violation
from events import emit
from hub import hub
from positions import evenly_spaced
from serializers import activity_out, card_out, column_out, member_out

DEFAULT_COLUMNS = ["À faire", "En cours", "Terminé"]

router = APIRouter()


@router.get("/api/boards")
def list_boards(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    memberships = (
        db.query(models.Membership)
        .options(selectinload(models.Membership.user))
        .filter_by(user_id=user.id)
        .all()
    )
    board_ids = [m.board_id for m in memberships]
    if not board_ids:
        return []
    boards = {b.id: b for b in db.query(models.Board).filter(models.Board.id.in_(board_ids))}
    card_counts = dict(
        db.query(models.Card.board_id, func.count(models.Card.id))
        .filter(models.Card.board_id.in_(board_ids))
        .group_by(models.Card.board_id)
    )
    members: dict[int, list] = {}
    for m in (
        db.query(models.Membership)
        .options(selectinload(models.Membership.user))
        .filter(models.Membership.board_id.in_(board_ids))
        .order_by(models.Membership.id)  # sans ORDER BY, Postgres ne garantit aucun ordre
    ):
        members.setdefault(m.board_id, []).append(member_out(m))

    # Tri sur la date elle-même (et non sa chaîne ISO), départagé par id : ordre stable quel que
    # soit l'ordre de lecture des lignes
    rows = sorted(
        ((m, b) for m in memberships if (b := boards.get(m.board_id))),
        key=lambda mb: (mb[1].created_at, mb[1].id), reverse=True,
    )
    return [
        {
            "id": b.id, "name": b.name, "role": m.role, "created_at": b.created_at.isoformat(),
            "card_count": card_counts.get(b.id, 0), "members": members.get(b.id, []),
        }
        for m, b in rows
    ]


@router.post("/api/boards", status_code=201)
def create_board(payload: schemas.BoardIn, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    board = models.Board(name=payload.name, owner_id=user.id)
    board.memberships.append(models.Membership(user_id=user.id, role=models.OWNER))
    for name, pos in zip(DEFAULT_COLUMNS, evenly_spaced(len(DEFAULT_COLUMNS))):
        board.columns.append(models.BoardColumn(name=name, position=pos))
    db.add(board)
    db.flush()
    db.add(models.Activity(board_id=board.id, user_id=user.id, message="a créé le tableau"))
    db.commit()
    return {"id": board.id, "name": board.name}


@router.get("/api/boards/{board_id}")
def get_board(board_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    board = board_for(db, user, board_id)
    columns = (
        db.query(models.BoardColumn)
        .options(selectinload(models.BoardColumn.cards))
        .filter_by(board_id=board.id)
        .order_by(models.BoardColumn.position, models.BoardColumn.id)
        .all()
    )
    memberships = (
        db.query(models.Membership).options(selectinload(models.Membership.user)).filter_by(board_id=board.id).all()
    )
    role = next(m.role for m in memberships if m.user_id == user.id)
    return {
        "id": board.id, "name": board.name, "revision": board.revision, "role": role,
        "members": sorted((member_out(m) for m in memberships), key=lambda m: (m["role"] != models.OWNER, m["name"])),
        "columns": [column_out(c) for c in columns],
    }


@router.patch("/api/boards/{board_id}")
def rename_board(
    board_id: int, payload: schemas.BoardIn, background: BackgroundTasks,
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db),
):
    board = board_for(db, user, board_id)
    lock_board(db, board.id)
    if board.name == payload.name:
        return {"id": board.id, "name": board.name}
    board.name = payload.name
    emit(db, background, board, user, "board.updated", lambda: {"name": board.name},
         f"a renommé le tableau en « {board.name} »")
    return {"id": board.id, "name": board.name}


@router.delete("/api/boards/{board_id}", status_code=204)
def delete_board(
    board_id: int, background: BackgroundTasks,
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db),
):
    board = board_for(db, user, board_id, owner_only=True)
    # Attend la fin des écritures en cours sur ce tableau avant de le supprimer en cascade
    lock_board(db, board.id)
    db.delete(board)
    db.commit()
    background.add_task(hub.publish, board_id, {"type": "board.deleted", "actor_id": user.id, "payload": {}})
    background.add_task(hub.close_room, board_id)
    return None


@router.get("/api/boards/{board_id}/activity")
def board_activity(
    board_id: int, limit: int = Query(40, ge=1, le=200),
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db),
):
    board = board_for(db, user, board_id)
    rows = (
        db.query(models.Activity)
        .options(selectinload(models.Activity.user))
        .filter_by(board_id=board.id)
        .order_by(models.Activity.created_at.desc(), models.Activity.id.desc())
        .limit(limit)
        .all()
    )
    return [activity_out(a) for a in rows]

@router.post("/api/boards/{board_id}/members", status_code=201)
def add_member(
    board_id: int, payload: schemas.MemberIn, background: BackgroundTasks,
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db),
):
    board = board_for(db, user, board_id, owner_only=True)
    lock_board(db, board.id)
    invitee = db.query(models.User).filter_by(email=payload.email.lower()).first()
    if not invitee:
        raise HTTPException(404, "Aucun compte avec cet e-mail : la personne doit d'abord s'inscrire")
    if membership_of(db, board.id, invitee.id):
        raise HTTPException(409, f"{invitee.name} fait déjà partie du tableau")
    invitee_name = invitee.name
    membership = models.Membership(board_id=board.id, user_id=invitee.id, role=models.MEMBER)
    db.add(membership)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        # Invitation simultanée de la même personne : la contrainte unique (board_id, user_id) tranche
        if is_unique_violation(exc):
            raise HTTPException(409, f"{invitee_name} fait déjà partie du tableau")
        # Clé étrangère : le tableau a été supprimé entre-temps (possible en SQLite, où lock_board
        # ne verrouille rien)
        if db.get(models.Board, board_id) is None:
            raise HTTPException(404, "Tableau introuvable")
        raise
    return emit(db, background, board, user, "member.added", lambda: member_out(membership), f"a invité {invitee.name}")


@router.delete("/api/boards/{board_id}/members/{member_id}", status_code=204)
def remove_member(
    board_id: int, member_id: int, background: BackgroundTasks,
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db),
):
    leaving_self = member_id == user.id
    board = board_for(db, user, board_id, owner_only=not leaving_self)
    lock_board(db, board.id)
    membership = membership_of(db, board.id, member_id)
    if not membership:
        raise HTTPException(404, "Membre introuvable")
    if membership.role == models.OWNER:
        raise HTTPException(409, "Le propriétaire ne peut pas quitter son tableau (il peut le supprimer)")

    name = membership.user.name
    db.delete(membership)
    db.flush()
    # Les cartes assignées à la personne retirée redeviennent non assignées. La version est
    # incrémentée en SQL (comme dans update_card) pour ne pas écraser une édition concurrente.
    ids = list(db.scalars(select(models.Card.id).filter_by(board_id=board.id, assignee_id=member_id)))
    if ids:
        db.execute(
            update(models.Card)
            .where(models.Card.id.in_(ids), models.Card.assignee_id == member_id)
            .values(assignee_id=None, version=models.Card.version + 1)
            .execution_options(synchronize_session=False)
        )
    unassigned = db.scalars(
        select(models.Card).where(models.Card.id.in_(ids)).order_by(models.Card.id)
        .execution_options(populate_existing=True)
    ).all()
    message = "a quitté le tableau" if leaving_self else f"a retiré {name} du tableau"
    emit(db, background, board, user, "member.removed",
         lambda: {"user_id": member_id, "cards": [card_out(c) for c in unassigned]}, message)
    background.add_task(hub.kick, board.id, member_id)
    return None
