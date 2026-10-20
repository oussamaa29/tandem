import logging
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("tandem.config")

# Secret de développement : pratique en local et pour les tests, jamais en production
DEV_JWT_SECRET = "dev-only-secret-change-me-in-production-please"
# Valeurs d'exemple (.env.example) considérées comme « pas de vrai secret »
_PLACEHOLDER_SECRETS = {DEV_JWT_SECRET, "change-me"}


class Settings:
    APP_ENV: str = os.getenv("APP_ENV", "development").lower()
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./tandem.db")
    JWT_SECRET: str = os.getenv("JWT_SECRET") or DEV_JWT_SECRET
    JWT_EXPIRE_HOURS: int = int(os.getenv("JWT_EXPIRE_HOURS", "72"))
    CORS_ORIGINS: list = os.getenv("CORS_ORIGINS", "http://localhost:5176").split(",")
    # Diffusion entre plusieurs instances (pub/sub + présence partagée) ; absent : tout en mémoire
    REDIS_URL: str | None = os.getenv("REDIS_URL") or None
    # Adresses publiques : retour OAuth ({PUBLIC_API_URL}/api/auth/<fournisseur>/callback) et
    # redirection finale vers l'application
    PUBLIC_API_URL: str = os.getenv("PUBLIC_API_URL", "http://localhost:8040").rstrip("/")
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:5176").rstrip("/")
    # « Continuer avec Google / GitHub » : actif seulement si identifiant ET secret sont définis
    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    GITHUB_CLIENT_ID: str = os.getenv("GITHUB_CLIENT_ID", "")
    GITHUB_CLIENT_SECRET: str = os.getenv("GITHUB_CLIENT_SECRET", "")


def check_secret(s: "Settings") -> None:
    """Refuse de démarrer en production avec le secret de développement (les jetons seraient
    falsifiables par n'importe qui) ; se contente d'un avertissement en local."""
    if s.JWT_SECRET not in _PLACEHOLDER_SECRETS:
        return
    if s.APP_ENV == "production":
        raise RuntimeError("JWT_SECRET doit être défini quand APP_ENV=production")
    logger.warning("JWT_SECRET non défini : secret de développement utilisé (à ne jamais faire en production)")


settings = Settings()
