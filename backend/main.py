import logging
from contextlib import asynccontextmanager
from typing import Callable

import anyio
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

import models
import schemas
from auth import create_token, get_current_user, hash_password, user_from_token, verify_password
from config import check_secret, settings
from database import SessionLocal, get_db, is_sqlite, is_unique_violation, upgrade_schema
from hub import hub
from positions import evenly_spaced, position_at

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tandem")
check_secret(settings)

# Délai laissé au client pour s'authentifier après l'ouverture du WebSocket
WS_AUTH_TIMEOUT = 5.0


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Le schéma est géré par Alembic (`alembic upgrade head`). En SQLite (développement local, une
    # seule instance), on l'applique au démarrage pour garder un lancement sans étape préalable.
    # Jamais en Postgres : plusieurs instances migreraient en même temps, et une migration de
    # production doit être une étape explicite du déploiement, pas un effet de bord du démarrage.
    if is_sqlite:
        await run_in_threadpool(upgrade_schema)
    # Avec REDIS_URL : abonnement aux autres instances et battement de présence ; sinon rien
    await hub.start()
    try:
        yield
    finally:
        await hub.stop()


app = FastAPI(title="Tandem — Kanban collaboratif en temps réel", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEFAULT_COLUMNS = ["À faire", "En cours", "Terminé"]


@app.get("/health")
def health():
    return {"status": "healthy"}


# ---------------- Sérialisation ----------------

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


# ---------------- Accès ----------------

def _membership(db: Session, board_id: int, user_id: int) -> models.Membership | None:
    return db.query(models.Membership).filter_by(board_id=board_id, user_id=user_id).first()


def board_for(db: Session, user: models.User, board_id: int, owner_only: bool = False) -> models.Board:
    board = db.get(models.Board, board_id)
    membership = board and _membership(db, board.id, user.id)
    # 404 et non 403 : on ne révèle pas l'existence des tableaux des autres
    if not board or not membership:
        raise HTTPException(404, "Tableau introuvable")
    if owner_only and membership.role != models.OWNER:
        raise HTTPException(403, "Action réservée au propriétaire du tableau")
    return board


def column_for(db: Session, user: models.User, column_id: int) -> tuple[models.BoardColumn, models.Board]:
    col = db.get(models.BoardColumn, column_id)
    if not col or not _membership(db, col.board_id, user.id):
        raise HTTPException(404, "Colonne introuvable")
    return col, db.get(models.Board, col.board_id)


def card_for(db: Session, user: models.User, card_id: int) -> tuple[models.Card, models.Board]:
    card = db.get(models.Card, card_id)
    if not card or not _membership(db, card.board_id, user.id):
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


# ---------------- Diffusion ----------------

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


# ---------------- Auth ----------------

@app.post("/api/auth/register", status_code=201)
def register(payload: schemas.RegisterIn, db: Session = Depends(get_db)):
    email = payload.email.lower()
    if db.query(models.User).filter_by(email=email).first():
        raise HTTPException(409, "Un compte existe déjà avec cet e-mail")
    count = db.query(func.count(models.User.id)).scalar()
    user = models.User(
        email=email, name=payload.name, password_hash=hash_password(payload.password),
        color=models.AVATAR_COLORS[count % len(models.AVATAR_COLORS)],
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # Deux inscriptions simultanées avec le même e-mail : l'index unique tranche
        if is_unique_violation(exc):
            raise HTTPException(409, "Un compte existe déjà avec cet e-mail")
        raise
    db.refresh(user)
    return {"token": create_token(user.id), "user": user_out(user)}


@app.post("/api/auth/login")
def login(payload: schemas.LoginIn, db: Session = Depends(get_db)):
    user = db.query(models.User).filter_by(email=payload.email.lower()).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "E-mail ou mot de passe incorrect")
    return {"token": create_token(user.id), "user": user_out(user)}


@app.get("/api/me")
def me(user: models.User = Depends(get_current_user)):
    return user_out(user)


# ---------------- Tableaux ----------------

@app.get("/api/boards")
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


@app.post("/api/boards", status_code=201)
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


@app.get("/api/boards/{board_id}")
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


@app.patch("/api/boards/{board_id}")
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


@app.delete("/api/boards/{board_id}", status_code=204)
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


@app.get("/api/boards/{board_id}/activity")
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


# ---------------- Membres ----------------

@app.post("/api/boards/{board_id}/members", status_code=201)
def add_member(
    board_id: int, payload: schemas.MemberIn, background: BackgroundTasks,
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db),
):
    board = board_for(db, user, board_id, owner_only=True)
    lock_board(db, board.id)
    invitee = db.query(models.User).filter_by(email=payload.email.lower()).first()
    if not invitee:
        raise HTTPException(404, "Aucun compte avec cet e-mail : la personne doit d'abord s'inscrire")
    if _membership(db, board.id, invitee.id):
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


@app.delete("/api/boards/{board_id}/members/{member_id}", status_code=204)
def remove_member(
    board_id: int, member_id: int, background: BackgroundTasks,
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db),
):
    leaving_self = member_id == user.id
    board = board_for(db, user, board_id, owner_only=not leaving_self)
    lock_board(db, board.id)
    membership = _membership(db, board.id, member_id)
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


# ---------------- Colonnes ----------------

@app.post("/api/boards/{board_id}/columns", status_code=201)
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


@app.patch("/api/columns/{column_id}")
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


@app.post("/api/columns/{column_id}/move")
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


@app.delete("/api/columns/{column_id}", status_code=204)
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


# ---------------- Cartes ----------------

@app.post("/api/columns/{column_id}/cards", status_code=201)
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


@app.patch("/api/cards/{card_id}")
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
    if changes.get("assignee_id") is not None and not _membership(db, board.id, changes["assignee_id"]):
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


@app.post("/api/cards/{card_id}/move")
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


@app.delete("/api/cards/{card_id}", status_code=204)
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


# ---------------- WebSocket ----------------

def _socket_identity(board_id: int, token: str | None) -> tuple[tuple[int, str, str] | None, bool]:
    """(identité, est membre ?) pour un jeton. Synchrone : exécuté hors de la boucle d'événements."""
    db = SessionLocal()
    try:
        user = user_from_token(db, token)
        if user is None:
            return None, False
        return (user.id, user.name, user.color), _membership(db, board_id, user.id) is not None
    finally:
        db.close()


def _is_member(board_id: int, user_id: int) -> bool:
    db = SessionLocal()
    try:
        return _membership(db, board_id, user_id) is not None
    finally:
        db.close()


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


@app.websocket("/ws/boards/{board_id}")
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
