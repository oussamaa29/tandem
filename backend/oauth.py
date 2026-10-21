"""« Continuer avec Google / GitHub » : flux OAuth 2.0 « authorization code » côté serveur.

1. /api/auth/<fournisseur>/start?nonce=… : redirige vers le fournisseur avec un `state` aléatoire
   et un défi PKCE (S256). `state`, le vérificateur PKCE et le nonce de l'onglet (aléatoire, gardé
   en sessionStorage par le frontend) sont gardés dans un cookie HttpOnly signé, valable 10
   minutes : rien n'est stocké côté serveur, n'importe quelle instance peut traiter le retour.
2. /api/auth/<fournisseur>/callback : vérifie `state` (cookie ⇄ paramètre, comparaison à temps
   constant), échange le code contre des jetons (avec le vérificateur PKCE), en tire l'identité
   (sujet, e-mail vérifié, nom), puis trouve ou crée le compte.
3. Le jeton de session n'apparaît jamais dans une URL : le frontend reçoit un code de connexion
   à usage unique (60 s, seul son condensé est en base) qu'il échange par POST /api/auth/exchange
   AVEC le nonce. Un code intercepté ou fabriqué par quelqu'un d'autre (qui voudrait connecter sa
   victime à son propre compte : « login CSRF ») est donc inutilisable dans un autre onglet.

Seule différence entre fournisseurs : l'obtention de l'identité. Google (OpenID Connect) la donne
dans un id_token signé, vérifié ici (signature RS256 via les clés publiques de Google, émetteur,
audience, expiration) ; GitHub (OAuth simple) via son API, en ne retenant que l'adresse e-mail
principale ET vérifiée.
"""

import base64
import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import jwt
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
from config import settings
from database import is_unique_violation

logger = logging.getLogger("tandem.oauth")

STATE_COOKIE = "tandem_oauth"
STATE_TTL = timedelta(minutes=10)
STATE_AUDIENCE = "tandem:oauth-state"  # un cookie d'état n'est jamais accepté comme jeton de session
LOGIN_CODE_TTL = timedelta(seconds=60)
HTTP_TIMEOUT = 10.0
# Nonce de l'onglet : au moins 128 bits aléatoires, en caractères sûrs pour une URL
NONCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{22,128}")
# Tolérance d'horloge sur iat / exp des id_token Google (serveur légèrement en retard ou en avance)
CLOCK_LEEWAY_SECONDS = 60

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]
GITHUB_API = "https://api.github.com"


class OAuthError(Exception):
    """Échec présenté à l'utilisateur : `code` est repris dans #/connexion?erreur=<code>.

    annule : l'utilisateur a refusé chez le fournisseur · etat : state absent, expiré ou différent
    fournisseur : fournisseur injoignable ou réponse inattendue · jeton : code ou id_token refusé
    email_non_verifie : pas d'adresse vérifiée · non_configure · interne
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


@dataclass
class Identity:
    subject: str
    email: str | None
    email_verified: bool
    name: str


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------- HTTP vers les fournisseurs (remplacés dans les tests) ----------------

def _parse(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        raise OAuthError("fournisseur", f"réponse illisible (HTTP {response.status_code})")


def http_post(url: str, data: dict, headers: dict | None = None) -> tuple[int, dict]:
    try:
        response = httpx.post(url, data=data, headers={"Accept": "application/json", **(headers or {})},
                              timeout=HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise OAuthError("fournisseur", f"{url} : {exc}")
    return response.status_code, _parse(response)


def http_get(url: str, headers: dict) -> tuple[int, object]:
    try:
        response = httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    except httpx.HTTPError as exc:
        raise OAuthError("fournisseur", f"{url} : {exc}")
    return response.status_code, _parse(response)


# ---------------- Fournisseurs ----------------

class Provider:
    name: str
    label: str
    authorize_url: str
    token_url: str
    scope: str
    extra_authorize_params: dict = {}

    @property
    def client_id(self) -> str:
        return getattr(settings, f"{self.name.upper()}_CLIENT_ID")

    @property
    def client_secret(self) -> str:
        return getattr(settings, f"{self.name.upper()}_CLIENT_SECRET")

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def redirect_uri(self) -> str:
        return f"{settings.PUBLIC_API_URL}/api/auth/{self.name}/callback"

    def authorization_url(self, state: str, verifier: str) -> str:
        params = {
            "client_id": self.client_id, "redirect_uri": self.redirect_uri, "scope": self.scope,
            "state": state, "code_challenge": pkce_challenge(verifier), "code_challenge_method": "S256",
            **self.extra_authorize_params,
        }
        return f"{self.authorize_url}?{urlencode(params)}"

    def exchange(self, code: str, verifier: str) -> dict:
        status, body = http_post(self.token_url, {
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
            "client_id": self.client_id, "client_secret": self.client_secret, "code_verifier": verifier,
        })
        # GitHub répond 200 même en cas d'échec, avec un champ `error`
        if status != 200 or not isinstance(body, dict) or "error" in body:
            error = body.get("error") if isinstance(body, dict) else None
            raise OAuthError("jeton", f"échange du code refusé par {self.name} (HTTP {status}, {error})")
        return body

    def identity(self, tokens: dict) -> Identity:
        raise NotImplementedError


class Google(Provider):
    name = "google"
    label = "Google"
    authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    scope = "openid email profile"
    extra_authorize_params = {"response_type": "code", "prompt": "select_account"}

    def __init__(self):
        self._jwks: jwt.PyJWKClient | None = None

    @property
    def jwks(self) -> jwt.PyJWKClient:
        # Clés publiques de Google mises en cache (elles tournent rarement)
        if self._jwks is None:
            self._jwks = jwt.PyJWKClient(GOOGLE_JWKS_URL, cache_keys=True, lifespan=3600, timeout=int(HTTP_TIMEOUT))
        return self._jwks

    def identity(self, tokens: dict) -> Identity:
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str):
            raise OAuthError("jeton", "id_token absent")
        try:
            key = self.jwks.get_signing_key_from_jwt(id_token).key
            claims = jwt.decode(
                id_token, key, algorithms=["RS256"], audience=self.client_id, issuer=GOOGLE_ISSUERS,
                options={"require": ["iss", "aud", "exp", "iat", "sub"]}, leeway=CLOCK_LEEWAY_SECONDS,
            )
        except jwt.PyJWTError as exc:  # signature, émetteur, audience, expiration, clés injoignables…
            raise OAuthError("jeton", f"id_token Google refusé : {exc}")
        return Identity(
            subject=str(claims["sub"]), email=claims.get("email"),
            email_verified=claims.get("email_verified") in (True, "true"), name=claims.get("name") or "",
        )


class GitHub(Provider):
    name = "github"
    label = "GitHub"
    authorize_url = "https://github.com/login/oauth/authorize"
    token_url = "https://github.com/login/oauth/access_token"
    scope = "read:user user:email"

    def identity(self, tokens: dict) -> Identity:
        access_token = tokens.get("access_token")
        if not isinstance(access_token, str):
            raise OAuthError("jeton", "access_token GitHub absent")
        headers = {
            "Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Tandem",
        }
        status, user = http_get(f"{GITHUB_API}/user", headers)
        if status != 200 or not isinstance(user, dict) or "id" not in user:
            raise OAuthError("fournisseur", f"profil GitHub illisible (HTTP {status})")
        status, emails = http_get(f"{GITHUB_API}/user/emails", headers)
        if status != 200 or not isinstance(emails, list):
            raise OAuthError("fournisseur", f"adresses GitHub illisibles (HTTP {status})")
        # Uniquement l'adresse principale, et seulement si GitHub l'a vérifiée
        primary = next((e for e in emails if isinstance(e, dict) and e.get("primary")), None)
        return Identity(
            subject=str(user["id"]), email=primary.get("email") if primary else None,
            email_verified=bool(primary and primary.get("verified") is True),
            name=user.get("name") or user.get("login") or "",
        )


PROVIDERS: dict[str, Provider] = {p.name: p for p in (Google(), GitHub())}


# ---------------- state + PKCE ----------------

def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def valid_nonce(nonce: str | None) -> bool:
    return bool(nonce and NONCE_PATTERN.fullmatch(nonce))


def new_flow(provider: str, nonce: str) -> tuple[str, str, str]:
    """(state, vérificateur PKCE, valeur du cookie signé qui les transporte avec le nonce)."""
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    cookie = jwt.encode(
        {"prv": provider, "st": state, "cv": verifier, "nn": nonce, "aud": STATE_AUDIENCE,
         "exp": datetime.now(timezone.utc) + STATE_TTL},
        settings.JWT_SECRET, algorithm="HS256",
    )
    return state, verifier, cookie


def check_state(cookie: str | None, provider: str, state: str | None) -> tuple[str, str]:
    """Vérifie que le retour correspond au départ de CE navigateur ; renvoie (vérificateur PKCE, nonce)."""
    if not cookie or not state:
        raise OAuthError("etat", "cookie ou state absent")
    try:
        data = jwt.decode(cookie, settings.JWT_SECRET, algorithms=["HS256"], audience=STATE_AUDIENCE)
    except jwt.PyJWTError as exc:
        raise OAuthError("etat", f"cookie d'état invalide : {exc}")
    expected = data.get("st")
    if data.get("prv") != provider or not isinstance(expected, str) or not hmac.compare_digest(expected, state):
        raise OAuthError("etat", "state différent")
    if not valid_nonce(data.get("nn")):
        raise OAuthError("etat", "nonce absent du cookie d'état")
    return data["cv"], data["nn"]


# ---------------- Comptes ----------------

def resolve_user(db: Session, provider: str, identity: Identity) -> tuple[models.User, bool]:
    """(compte, sessions révoquées ?). Compte déjà lié à cette identité ; sinon compte existant
    avec la même adresse VÉRIFIÉE par le fournisseur (rattaché) ; sinon nouveau compte sans mot de
    passe.

    Une adresse confirmée par le fournisseur l'emporte sur un mot de passe : l'inscription par mot
    de passe ne vérifie pas l'adresse, n'importe qui a pu créer un compte au nom de la victime
    avant qu'elle n'arrive (« pré-création de compte »). Au premier rattachement d'un tel compte,
    son mot de passe est donc supprimé et toutes ses sessions sont fermées (token_version).

    Si le compte est déjà lié à un autre fournisseur, rien n'est effacé : ce lien a été créé à
    partir de la même adresse, déjà vérifiée par un fournisseur (soit à la création du compte,
    soit lors d'un rattachement qui a lui-même fait ce ménage), et un compte sans mot de passe ne
    peut pas en recevoir un. Ses sessions appartiennent donc déjà au propriétaire de l'adresse ;
    les fermer ne ferait que déconnecter la bonne personne quand elle ajoute un second fournisseur."""
    for attempt in range(2):
        link = db.query(models.OAuthAccount).filter_by(provider=provider, subject=identity.subject).first()
        if link:
            return link.user, False
        if not identity.email or not identity.email_verified:
            # Sans adresse vérifiée, rattacher à un compte existant permettrait de s'en emparer
            raise OAuthError("email_non_verifie")
        email = identity.email.lower()
        user = db.query(models.User).filter_by(email=email).first()
        revoked = False
        if user is None:
            name = identity.name.strip()[:80] or email.split("@")[0][:80]
            user = models.User(email=email, name=name, password_hash=None, color=models.next_avatar_color(db))
            db.add(user)
        elif user.password_hash and db.query(models.OAuthAccount.id).filter_by(user_id=user.id).first() is None:
            user.password_hash = None
            user.token_version = models.User.token_version + 1  # en SQL : sûr si deux rattachements se croisent
            revoked = True
        db.add(models.OAuthAccount(provider=provider, subject=identity.subject, user=user))
        try:
            db.commit()
            if revoked:
                logger.warning("Compte %s rattaché à %s par adresse vérifiée : mot de passe supprimé, "
                               "sessions fermées", user.id, provider)
            return user, revoked
        except IntegrityError as exc:
            db.rollback()
            # Première connexion simultanée (deux onglets) : l'autre requête a créé le compte ou
            # le lien entre-temps, le second passage les retrouve
            if attempt or not is_unique_violation(exc):
                raise
    raise AssertionError("inaccessible")


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def issue_login_code(db: Session, user_id: int, nonce: str) -> str:
    code = secrets.token_urlsafe(32)
    now = _now()
    db.execute(delete(models.LoginCode).where(models.LoginCode.expires_at < now))  # ménage des codes expirés
    db.add(models.LoginCode(
        code_hash=_hash_code(code), nonce_hash=_hash_code(nonce), user_id=user_id, expires_at=now + LOGIN_CODE_TTL,
    ))
    db.commit()
    return code


def redeem_login_code(db: Session, code: str, nonce: str | None) -> models.User | None:
    """Usage unique : DELETE … RETURNING atomique, deux échanges simultanés ne peuvent pas réussir.

    Le code est consommé à la PREMIÈRE tentative, même avec un nonce faux ou absent : un code
    intercepté ne laisse qu'un essai (pas de recherche du nonce par force brute, même s'il fait
    128 bits ou plus) et ne peut plus servir ensuite. Revers assumé : quelqu'un qui a intercepté
    le code peut le « brûler », le légitime n'a alors qu'à relancer la connexion."""
    row = db.execute(
        delete(models.LoginCode)
        .where(models.LoginCode.code_hash == _hash_code(code))
        .returning(models.LoginCode.user_id, models.LoginCode.expires_at, models.LoginCode.nonce_hash)
    ).first()
    db.commit()
    if row is None or row.expires_at < _now():
        return None
    if not nonce or not hmac.compare_digest(row.nonce_hash, _hash_code(nonce)):
        return None
    return db.get(models.User, row.user_id)
