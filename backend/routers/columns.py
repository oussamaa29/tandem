"""Colonnes : création, renommage, déplacement, suppression."""
from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

import models
import schemas
from access import board_for, column_for, lock_board
from auth import get_current_user
from database import get_db
from events import emit
from positions import evenly_spaced, position_at
from serializers import column_out

router = APIRouter()


@router.post("/api/boards/{board_id}/columns", status_code=201)
def create_column(
    board_id: int, payload: schemas.ColumnIn, background: BackgroundTasks,
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db),
):
    board = board_for(db, user, board_id)
    lock_board(db, board.id)
    last = db.query(func.max(models.BoardColumn.position)).filter_by(board_id=board.id).scalar()
    col = models.BoardColumn(board_id=board.id, name=payload.name, position=(last or 0) + 1024)
    db.add(col)
    db.flush()
    return emit(db, background, board, user, "column.created", lambda: column_out(col),
                f"a ajouté la colonne « {col.name} »")


@router.patch("/api/columns/{column_id}")
def rename_column(
    column_id: int, payload: schemas.ColumnIn, background: BackgroundTasks,
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db),
):
    col, board = column_for(db, user, column_id)
    lock_board(db, board.id)
    old = col.name
    if old == payload.name:
        return column_out(col, with_cards=False)
    col.name = payload.name
    return emit(db, background, board, user, "column.updated", lambda: column_out(col, with_cards=False),
                f"a renommé la colonne « {old} » en « {col.name} »")


@router.post("/api/columns/{column_id}/move")
def move_column(
    column_id: int, payload: schemas.MoveIn, background: BackgroundTasks,
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db),
):
    col, board = column_for(db, user, column_id)
    lock_board(db, board.id)
    others = (
        db.query(models.BoardColumn)
        .filter(models.BoardColumn.board_id == board.id, models.BoardColumn.id != col.id)
        .order_by(models.BoardColumn.position, models.BoardColumn.id)
        .all()
    )
    position = position_at([c.position for c in others], payload.index)
    if position is None:
        ordered = others[:]
        ordered.insert(min(payload.index, len(ordered)), col)
        for c, pos in zip(ordered, evenly_spaced(len(ordered))):
            c.position = pos
    else:
        col.position = position
    emit(db, background, board, user, "columns.reordered",
         lambda: {"columns": [{"id": c.id, "position": c.position} for c in [col, *others]]})
    return column_out(col, with_cards=False)


@router.delete("/api/columns/{column_id}", status_code=204)
def delete_column(
    column_id: int, background: BackgroundTasks,
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db),
):
    col, board = column_for(db, user, column_id)
    lock_board(db, board.id)
    name = col.name
    db.delete(col)
    db.flush()
    emit(db, background, board, user, "column.deleted", lambda: {"id": column_id},
         f"a supprimé la colonne « {name} »")
    return None
