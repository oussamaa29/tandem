"""Inscription, connexion par mot de passe et par Google / GitHub (voir oauth.py)."""
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
import oauth
import schemas
from auth import create_token, get_current_user, hash_password, verify_password
from config import settings
from database import get_db, is_unique_violation
from hub import hub
from serializers import user_out

logger = logging.getLogger("tandem")

router = APIRouter()


@router.post("/api/auth/register", status_code=201)
def register(payload: schemas.RegisterIn, db: Session = Depends(get_db)):
    email = payload.email.lower()
    if db.query(models.User).filter_by(email=email).first():
        raise HTTPException(409, "Un compte existe déjà avec cet e-mail")
    user = models.User(
        email=email, name=payload.name, password_hash=hash_password(payload.password),
        color=models.next_avatar_color(db),
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
    return {"token": create_token(user), "user": user_out(user)}


@router.post("/api/auth/login")
def login(payload: schemas.LoginIn, db: Session = Depends(get_db)):
    user = db.query(models.User).filter_by(email=payload.email.lower()).first()
    # Compte créé via Google / GitHub (sans mot de passe) : même réponse qu'un mauvais mot de
    # passe, pour ne pas révéler comment le compte a été créé
    if not user or not user.password_hash or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "E-mail ou mot de passe incorrect")
    return {"token": create_token(user), "user": user_out(user)}

def _oauth_provider(provider: str) -> oauth.Provider:
    p = oauth.PROVIDERS.get(provider)
    if p is None:
        raise HTTPException(404, "Fournisseur inconnu")
    return p


@router.get("/api/auth/providers")
def auth_providers():
    """Fournisseurs configurés : le frontend n'affiche que leurs boutons."""
    return {name: p.enabled for name, p in oauth.PROVIDERS.items()}


@router.get("/api/auth/{provider}/start")
def oauth_start(provider: str, nonce: str | None = None):
    """`nonce` : valeur aléatoire gardée par l'onglet qui lance la connexion (sessionStorage) ; le
    code de connexion final ne pourra être échangé qu'avec elle."""
    p = _oauth_provider(provider)
    if not p.enabled:
        raise HTTPException(503, f"Connexion avec {p.label} non configurée sur ce serveur")
    if not oauth.valid_nonce(nonce):
        raise HTTPException(400, "Lien de connexion invalide : relance la connexion depuis l'application")
    state, verifier, cookie = oauth.new_flow(p.name, nonce)
    response = RedirectResponse(p.authorization_url(state, verifier), status_code=302)
    # SameSite=Lax : envoyé lors du retour du fournisseur (navigation de premier niveau en GET)
    response.set_cookie(
        oauth.STATE_COOKIE, cookie, max_age=int(oauth.STATE_TTL.total_seconds()), path="/api/auth",
        httponly=True, samesite="lax", secure=settings.PUBLIC_API_URL.startswith("https://"),
    )
    return response


@router.get("/api/auth/{provider}/callback")
def oauth_callback(
    provider: str, request: Request, background: BackgroundTasks, code: str | None = None,
    state: str | None = None, error: str | None = None, db: Session = Depends(get_db),
):
    """Retour du fournisseur. Toujours une redirection vers l'application, jamais une erreur
    brute : #/auth/<fournisseur>?code=<code à usage unique> ou #/connexion?erreur=<code>."""
    p = _oauth_provider(provider)
    try:
        if not p.enabled:
            raise oauth.OAuthError("non_configure")
        if error:
            raise oauth.OAuthError("annule" if error == "access_denied" else "fournisseur", error)
        verifier, nonce = oauth.check_state(request.cookies.get(oauth.STATE_COOKIE), p.name, state)
        if not code:
            raise oauth.OAuthError("fournisseur", "code absent")
        user, revoked = oauth.resolve_user(db, p.name, p.identity(p.exchange(code, verifier)))
        if revoked:
            # Sessions fermées (token_version) : les WebSocket déjà ouverts au nom de ce compte aussi,
            # sur toutes les instances, sinon ils continueraient à recevoir les événements
            for board_id in db.scalars(select(models.Membership.board_id).filter_by(user_id=user.id)):
                background.add_task(hub.kick, board_id, user.id, 4401)
        login_code = oauth.issue_login_code(db, user.id, nonce)
        target = f"{settings.FRONTEND_URL}/#/auth/{p.name}?code={login_code}"
    except oauth.OAuthError as exc:
        logger.info("Connexion %s refusée (%s) : %s", p.name, exc.code, exc)
        target = f"{settings.FRONTEND_URL}/#/connexion?erreur={exc.code}"
    except Exception:
        logger.exception("Connexion %s : erreur inattendue", p.name)
        target = f"{settings.FRONTEND_URL}/#/connexion?erreur=interne"
    response = RedirectResponse(target, status_code=302)
    response.delete_cookie(oauth.STATE_COOKIE, path="/api/auth")  # state à usage unique
    return response


@router.post("/api/auth/exchange")
def oauth_exchange(payload: schemas.ExchangeIn, db: Session = Depends(get_db)):
    """Code de connexion à usage unique (60 s) + nonce de l'onglet qui a lancé la connexion →
    jeton de session, hors de toute URL."""
    user = oauth.redeem_login_code(db, payload.code, payload.nonce)
    if user is None:
        raise HTTPException(400, "Code de connexion invalide ou expiré")
    return {"token": create_token(user), "user": user_out(user)}


@router.get("/api/me")
def me(user: models.User = Depends(get_current_user)):
    return user_out(user)
