import logging

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from auth import hash_password, verify_password
from config import DEV_JWT_SECRET, Settings, check_secret
from database import engine


def test_long_multibyte_password_can_register_and_login(client):
    password = "é" * 40  # 40 caractères mais 80 octets : au-delà de la limite de bcrypt
    r = client.post("/api/auth/register", json={"email": "long@example.com", "name": "Long", "password": password})
    assert r.status_code == 201, r.text
    assert client.post("/api/auth/login", json={"email": "long@example.com", "password": password}).status_code == 200
    # Même préfixe de 72 octets : doit rester refusé (pas de troncature silencieuse)
    r = client.post("/api/auth/login", json={"email": "long@example.com", "password": password[:36] + "x" * 4})
    assert r.status_code == 401


def test_concurrent_registration_with_same_email_is_409(client):
    """Inscription concurrente validée entre la vérification de l'e-mail et l'insertion : c'est
    l'index unique qui refuse, et la réponse reste un 409, pas une erreur 500."""
    fired = []

    @event.listens_for(Session, "before_flush")
    def register_concurrently(session, _ctx, _instances):
        if fired:
            return
        fired.append(True)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (email, name, password_hash, color) "
                              "VALUES ('dup@example.com', 'Autre', 'h', '#000000')"))

    try:
        r = client.post("/api/auth/register", json={"email": "dup@example.com", "name": "Dup", "password": "motdepasse123"})
    finally:
        event.remove(Session, "before_flush", register_concurrently)
    assert fired
    assert r.status_code == 409


def test_login_with_very_long_password_is_401_not_500(client, alice):
    r = client.post("/api/auth/login", json={"email": "alice@example.com", "password": "x" * 1000})
    assert r.status_code == 401


def test_verify_password_never_raises():
    assert verify_password("motdepasse123", hash_password("motdepasse123"))
    assert not verify_password("motdepasse123", "pas-un-hash-bcrypt")


def _settings(secret, env):
    s = Settings()
    s.JWT_SECRET, s.APP_ENV = secret, env
    return s


def test_production_refuses_default_secret(caplog):
    with pytest.raises(RuntimeError):
        check_secret(_settings(DEV_JWT_SECRET, "production"))
    with pytest.raises(RuntimeError):
        check_secret(_settings("change-me", "production"))
    check_secret(_settings("un-vrai-secret-de-production", "production"))

    with caplog.at_level(logging.WARNING, logger="tandem.config"):
        check_secret(_settings(DEV_JWT_SECRET, "development"))
    assert "JWT_SECRET" in caplog.text
