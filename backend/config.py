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


def check_secret(s: "Settings") -> None:
    """Refuse de démarrer en production avec le secret de développement (les jetons seraient
    falsifiables par n'importe qui) ; se contente d'un avertissement en local."""
    if s.JWT_SECRET not in _PLACEHOLDER_SECRETS:
        return
    if s.APP_ENV == "production":
        raise RuntimeError("JWT_SECRET doit être défini quand APP_ENV=production")
    logger.warning("JWT_SECRET non défini : secret de développement utilisé (à ne jamais faire en production)")


settings = Settings()
