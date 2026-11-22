"""Conversion des modèles en JSON pour l'API et les événements temps réel."""


import models


def user_out(u: models.User) -> dict:
    return {"id": u.id, "name": u.name, "email": u.email, "color": u.color}


def member_out(m: models.Membership) -> dict:
    return {**user_out(m.user), "role": m.role}


def card_out(c: models.Card) -> dict:
    return {
        "id": c.id, "column_id": c.column_id, "title": c.title, "description": c.description,
        "position": c.position, "assignee_id": c.assignee_id, "version": c.version,
        "created_by_id": c.created_by_id, "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def column_out(col: models.BoardColumn, with_cards: bool = True) -> dict:
    out = {"id": col.id, "name": col.name, "position": col.position}
    if with_cards:
        out["cards"] = [card_out(c) for c in col.cards]
    return out


def activity_out(a: models.Activity) -> dict:
    return {
        "id": a.id, "message": a.message, "created_at": a.created_at.isoformat(),
        "user": {"id": a.user.id, "name": a.user.name, "color": a.user.color},
    }
