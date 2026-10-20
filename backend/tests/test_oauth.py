"""Connexion avec Google / GitHub, sans réseau : le point de jeton, l'API GitHub et les clés
publiques de Google sont simulés ; les id_token sont signés avec une clé RSA générée ici."""

import time
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import oauth
from config import settings

GOOGLE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
FRONTEND = "http://localhost:5176"


class FakeProviders:
    """Réponses des fournisseurs ; `identity` règle ce que Google ou GitHub renverront."""

    def __init__(self):
        self.token_status = 200
        self.token_body: dict | None = None  # None : réponse normale selon le fournisseur
        self.id_claims: dict = {}
        self.id_key = GOOGLE_KEY
        self.github_user: dict = {}
        self.github_emails: list = []
        self.token_requests: list[dict] = []

    def identity(self, provider, sub, email, verified=True, name="Gaby Martin"):
        if provider == "google":
            now = int(time.time())
            self.id_claims = {
                "iss": "https://accounts.google.com", "aud": settings.GOOGLE_CLIENT_ID, "sub": sub,
                "email": email, "email_verified": verified, "name": name, "iat": now, "exp": now + 300,
            }
        else:
            self.github_user = {"id": int(sub), "login": "gaby", "name": name}
            self.github_emails = [
                {"email": "autre@example.com", "primary": False, "verified": True},
                {"email": email, "primary": True, "verified": verified},
            ]

    def post(self, url, data, headers=None):
        self.token_requests.append({"url": url, **data})
        if self.token_body is not None:
            return self.token_status, self.token_body
        if url == oauth.PROVIDERS["google"].token_url:
            id_token = jwt.encode(self.id_claims, self.id_key, algorithm="RS256", headers={"kid": "test"})
            return self.token_status, {"access_token": "ya29", "id_token": id_token, "token_type": "Bearer"}
        return self.token_status, {"access_token": "gho_test", "token_type": "bearer", "scope": "read:user,user:email"}

    def get(self, url, headers):
        assert headers["Authorization"] == "Bearer gho_test" and headers["User-Agent"]
        if url.endswith("/user"):
            return 200, self.github_user
        return 200, self.github_emails


class FakeJWKS:
    """Clé publique de Google, comme la renverrait PyJWKClient."""

    def get_signing_key_from_jwt(self, token):
        return jwt.PyJWK.from_dict({**jwt.algorithms.RSAAlgorithm.to_jwk(GOOGLE_KEY.public_key(), as_dict=True),
                                    "kid": "test", "alg": "RS256"})


@pytest.fixture
def fake(monkeypatch):
    for name, value in {
        "GOOGLE_CLIENT_ID": "google-client.apps.googleusercontent.com", "GOOGLE_CLIENT_SECRET": "g-secret",
        "GITHUB_CLIENT_ID": "gh-client", "GITHUB_CLIENT_SECRET": "gh-secret",
        "PUBLIC_API_URL": "http://localhost:8040", "FRONTEND_URL": FRONTEND,
    }.items():
        monkeypatch.setattr(settings, name, value)
    providers = FakeProviders()
    monkeypatch.setattr(oauth, "http_post", providers.post)
    monkeypatch.setattr(oauth, "http_get", providers.get)
    monkeypatch.setattr(oauth.PROVIDERS["google"], "_jwks", FakeJWKS())
    return providers


def start(client, provider):
    r = client.get(f"/api/auth/{provider}/start", follow_redirects=False)
    assert r.status_code == 302, r.text
    return r, {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}


def callback(client, provider, **params):
    r = client.get(f"/api/auth/{provider}/callback", params=params, follow_redirects=False)
    assert r.status_code == 302, r.text
    location = r.headers["location"]
    assert location.startswith(f"{FRONTEND}/#/")
    route, _, query = location.split("#/", 1)[1].partition("?")
    return route, {k: v[0] for k, v in parse_qs(query).items()}


def sign_in(client, provider):
    """Parcours complet : départ, retour du fournisseur, échange du code. Renvoie la réponse finale."""
    _, params = start(client, provider)
    route, query = callback(client, provider, code="code-fournisseur", state=params["state"])
    assert route == f"auth/{provider}", query
    return client.post("/api/auth/exchange", json={"code": query["code"]})


def failure(client, provider, **callback_params):
    _, params = start(client, provider)
    route, query = callback(client, provider, **{"code": "code-fournisseur", "state": params["state"], **callback_params})
    assert route == "connexion"
    return query["erreur"]


# ---------------- Configuration ----------------

def test_providers_endpoint_and_not_configured(client, monkeypatch):
    for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"):
        monkeypatch.setattr(settings, name, "")
    assert client.get("/api/auth/providers").json() == {"google": False, "github": False}
    for provider in ("google", "github"):
        assert client.get(f"/api/auth/{provider}/start", follow_redirects=False).status_code == 503
        route, query = callback(client, provider, code="x", state="y")
        assert (route, query["erreur"]) == ("connexion", "non_configure")
    # Identifiant sans secret : toujours désactivé
    monkeypatch.setattr(settings, "GITHUB_CLIENT_ID", "gh-client")
    assert client.get("/api/auth/providers").json() == {"google": False, "github": False}
    assert client.get("/api/auth/apple/start", follow_redirects=False).status_code == 404


def test_providers_endpoint_when_configured(client, fake):
    assert client.get("/api/auth/providers").json() == {"google": True, "github": True}


@pytest.mark.parametrize("provider, host, scope", [
    ("google", "accounts.google.com", "openid email profile"),
    ("github", "github.com", "read:user user:email"),
])
def test_start_redirects_with_state_pkce_and_signed_cookie(client, fake, provider, host, scope):
    r, params = start(client, provider)
    assert urlsplit(r.headers["location"]).hostname == host
    assert params["redirect_uri"] == f"http://localhost:8040/api/auth/{provider}/callback"
    assert params["scope"] == scope and params["client_id"] == getattr(settings, f"{provider.upper()}_CLIENT_ID")
    assert params["code_challenge_method"] == "S256" and len(params["state"]) >= 32
    if provider == "google":
        assert params["response_type"] == "code" and params["prompt"] == "select_account"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/api/auth" in cookie and "max-age=600" in cookie
    assert "secure" not in cookie  # PUBLIC_API_URL en http (local)

    # Le vérificateur envoyé au point de jeton correspond au défi de l'URL d'autorisation
    fake.identity(provider, "1001", "gaby@example.com")
    callback(client, provider, code="code-fournisseur", state=params["state"])
    assert oauth.pkce_challenge(fake.token_requests[-1]["code_verifier"]) == params["code_challenge"]
    # Et le cookie d'état est effacé : il ne sert qu'une fois
    assert oauth.STATE_COOKIE not in client.cookies


def test_cookie_is_secure_when_api_is_https(client, fake, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://api.tandem.example")
    r = client.get("/api/auth/google/start", follow_redirects=False)
    assert "secure" in r.headers["set-cookie"].lower()


# ---------------- Comptes ----------------

@pytest.mark.parametrize("provider", ["google", "github"])
def test_first_sign_in_creates_a_passwordless_account(client, fake, provider):
    fake.identity(provider, "1001", "Gaby@Example.com", name="Gaby Martin")
    r = sign_in(client, provider)
    assert r.status_code == 200, r.text
    user = r.json()["user"]
    assert (user["name"], user["email"]) == ("Gaby Martin", "gaby@example.com")
    assert client.get("/api/me", headers={"Authorization": f"Bearer {r.json()['token']}"}).json()["id"] == user["id"]

    # Pas de mot de passe : connexion classique refusée avec le message habituel (aucune énumération)
    wrong = client.post("/api/auth/login", json={"email": "gaby@example.com", "password": "motdepasse123"})
    unknown = client.post("/api/auth/login", json={"email": "personne@example.com", "password": "motdepasse123"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    # S'inscrire avec un mot de passe sur la même adresse : toujours 409
    r = client.post("/api/auth/register", json={"email": "gaby@example.com", "name": "Gaby", "password": "motdepasse123"})
    assert r.status_code == 409


def test_github_name_falls_back_to_login(client, fake):
    fake.identity("github", "42", "gaby@example.com", name=None)
    assert sign_in(client, "github").json()["user"]["name"] == "gaby"


@pytest.mark.parametrize("provider", ["google", "github"])
def test_existing_password_account_is_linked_by_verified_email(client, fake, alice, provider):
    fake.identity(provider, "2002", "ALICE@example.com", name="Alice G.")
    r = sign_in(client, provider)
    assert r.json()["user"]["id"] == alice.id and r.json()["user"]["name"] == "Alice"
    # Le mot de passe reste utilisable
    assert client.post("/api/auth/login", json={"email": "alice@example.com", "password": "motdepasse123"}).status_code == 200


@pytest.mark.parametrize("provider", ["google", "github"])
def test_next_sign_in_uses_the_link_even_if_the_email_changed(client, fake, provider):
    fake.identity(provider, "3003", "gaby@example.com")
    first = sign_in(client, provider).json()["user"]["id"]
    fake.identity(provider, "3003", "gaby.nouvelle@example.com")
    assert sign_in(client, provider).json()["user"]["id"] == first


def test_same_account_can_link_google_and_github(client, fake, alice):
    fake.identity("google", "g-1", "alice@example.com")
    fake.identity("github", "77", "alice@example.com")
    assert sign_in(client, "google").json()["user"]["id"] == alice.id
    assert sign_in(client, "github").json()["user"]["id"] == alice.id
    # Les deux liens sont réutilisés ensuite, même si les adresses divergent
    fake.identity("google", "g-1", "alice.google@example.com")
    fake.identity("github", "77", "alice.github@example.com")
    assert sign_in(client, "google").json()["user"]["id"] == alice.id
    assert sign_in(client, "github").json()["user"]["id"] == alice.id


@pytest.mark.parametrize("provider", ["google", "github"])
def test_unverified_email_is_refused_and_creates_nothing(client, fake, alice, provider):
    # Adresse non vérifiée identique à un compte existant : rattacher reviendrait à le céder
    fake.identity(provider, "4004", "alice@example.com", verified=False)
    assert failure(client, provider) == "email_non_verifie"
    assert client.post("/api/auth/register", json={"email": "neuf@example.com", "name": "N", "password": "motdepasse123"}).json()["user"]["id"] == alice.id + 1


def test_github_without_primary_email_is_refused(client, fake):
    fake.identity("github", "5005", "gaby@example.com")
    fake.github_emails = [{"email": "gaby@example.com", "primary": False, "verified": True}]
    assert failure(client, "github") == "email_non_verifie"


# ---------------- id_token Google ----------------

@pytest.mark.parametrize("tamper", ["aud", "iss", "exp", "signature", "missing_sub"])
def test_invalid_google_id_token_is_refused(client, fake, tamper):
    fake.identity("google", "6006", "gaby@example.com")
    if tamper == "aud":
        fake.id_claims["aud"] = "autre-application.apps.googleusercontent.com"
    elif tamper == "iss":
        fake.id_claims["iss"] = "https://accounts.evil.example"
    elif tamper == "exp":
        fake.id_claims["exp"] = int(time.time()) - 60
    elif tamper == "signature":
        fake.id_key = OTHER_KEY
    else:
        del fake.id_claims["sub"]
    assert failure(client, "google") == "jeton"


def test_google_short_issuer_is_accepted(client, fake):
    fake.identity("google", "6007", "gaby@example.com")
    fake.id_claims["iss"] = "accounts.google.com"
    assert sign_in(client, "google").status_code == 200


# ---------------- Erreurs du parcours ----------------

@pytest.mark.parametrize("provider", ["google", "github"])
def test_token_endpoint_errors_are_refused(client, fake, provider):
    fake.identity(provider, "7007", "gaby@example.com")
    if provider == "github":
        fake.token_body = {"error": "bad_verification_code", "error_description": "The code passed is incorrect"}
    else:
        fake.token_status, fake.token_body = 400, {"error": "invalid_grant"}
    assert failure(client, provider) == "jeton"


def test_provider_unreachable_is_reported(client, fake, monkeypatch):
    def unreachable(url, data, headers=None):
        raise oauth.OAuthError("fournisseur", "délai dépassé")

    monkeypatch.setattr(oauth, "http_post", unreachable)
    assert failure(client, "github") == "fournisseur"


@pytest.mark.parametrize("provider", ["google", "github"])
def test_state_mismatch_or_missing_cookie_is_refused(client, fake, provider):
    fake.identity(provider, "8008", "gaby@example.com")
    assert failure(client, provider, state="un-autre-state") == "etat"
    # Sans le cookie du départ (autre navigateur, lien forgé)
    _, params = start(client, provider)
    client.cookies.clear()
    route, query = callback(client, provider, code="code-fournisseur", state=params["state"])
    assert (route, query["erreur"]) == ("connexion", "etat")
    # Cookie d'un départ vers un autre fournisseur
    _, params = start(client, "google" if provider == "github" else "github")
    route, query = callback(client, provider, code="code-fournisseur", state=params["state"])
    assert query["erreur"] == "etat"
    assert fake.token_requests == []  # jamais d'échange de code sans state valide


@pytest.mark.parametrize("provider", ["google", "github"])
def test_user_cancelling_at_provider_is_reported(client, fake, provider):
    _, params = start(client, provider)
    route, query = callback(client, provider, error="access_denied", state=params["state"])
    assert (route, query["erreur"]) == ("connexion", "annule")


def test_unexpected_error_redirects_instead_of_500(client, fake, monkeypatch):
    def broken(db, provider, identity):
        raise RuntimeError("base indisponible")

    fake.identity("google", "9009", "gaby@example.com")
    monkeypatch.setattr(oauth, "resolve_user", broken)
    assert failure(client, "google") == "interne"


# ---------------- Code de connexion ----------------

def test_login_code_is_single_use(client, fake):
    fake.identity("google", "1111", "gaby@example.com")
    _, params = start(client, "google")
    _, query = callback(client, "google", code="code-fournisseur", state=params["state"])
    assert client.post("/api/auth/exchange", json={"code": query["code"]}).status_code == 200
    replay = client.post("/api/auth/exchange", json={"code": query["code"]})
    assert replay.status_code == 400 and replay.json()["detail"] == "Code de connexion invalide ou expiré"
    assert client.post("/api/auth/exchange", json={"code": "invente"}).status_code == 400


def test_login_code_expires(client, fake, monkeypatch):
    monkeypatch.setattr(oauth, "LOGIN_CODE_TTL", timedelta(seconds=-1))
    fake.identity("github", "2222", "gaby@example.com")
    _, params = start(client, "github")
    _, query = callback(client, "github", code="code-fournisseur", state=params["state"])
    assert client.post("/api/auth/exchange", json={"code": query["code"]}).status_code == 400


def test_state_cookie_is_not_a_session_token(client, fake):
    r, _ = start(client, "google")
    cookie = r.cookies[oauth.STATE_COOKIE]
    assert client.get("/api/me", headers={"Authorization": f"Bearer {cookie}"}).status_code == 401
