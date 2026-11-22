import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from config import check_secret, settings
from database import is_sqlite, upgrade_schema
from hub import hub
from routers import auth as auth_routes
from routers import boards, cards, columns, realtime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tandem")
check_secret(settings)


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


@app.get("/health")
def health():
    return {"status": "healthy"}


for module in (auth_routes, boards, columns, cards, realtime):
    app.include_router(module.router)
