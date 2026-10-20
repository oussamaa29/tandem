from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from config import settings

is_sqlite = settings.DATABASE_URL.startswith("sqlite")
connect_args = {"check_same_thread": False} if is_sqlite else {}
engine = create_engine(settings.DATABASE_URL, connect_args=connect_args)

if is_sqlite:
    # SQLite n'applique les clés étrangères (et donc les ON DELETE CASCADE) que si on le demande
    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
