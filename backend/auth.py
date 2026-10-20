import base64
import hashlib
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

import models
from config import settings
from database import get_db

bearer = HTTPBearer(auto_error=False)


def _prehash(password: str) -> bytes:
    """bcrypt n'accepte que 72 octets (et bcrypt 5 lève une erreur au-delà) : on condense d'abord
    le mot de passe en SHA-256, encodé en base64 (44 octets, sans octet nul)."""
    return base64.b64encode(hashlib.sha256(password.encode()).digest())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    # Ne lève jamais : un hash corrompu ou une entrée invalide = mot de passe incorrect
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode())
    except ValueError:
        return False


def create_token(user_id: int) -> str:
    expires = datetime.now(timezone.utc) + timedelta(hours=settings.JWT_EXPIRE_HOURS)
    return jwt.encode({"sub": str(user_id), "exp": expires}, settings.JWT_SECRET, algorithm="HS256")


def user_from_token(db: Session, token: str | None) -> models.User | None:
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        return None
    return db.get(models.User, user_id)


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> models.User:
    user = user_from_token(db, creds.credentials if creds else None)
    if user is None:
        raise HTTPException(401, "Session invalide ou expirée")
    return user
