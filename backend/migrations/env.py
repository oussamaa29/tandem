from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

import models  # noqa: F401  (enregistre les tables dans Base.metadata)
from database import DATABASE_URL, Base

config = context.config

# En ligne de commande uniquement : appelé depuis l'application ou les tests, on garderait sinon
# la configuration de journalisation d'alembic.ini à la place de la leur
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql` : génère le SQL sans se connecter."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=DATABASE_URL.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Moteur dédié, sans le PRAGMA foreign_keys=ON de database.py : en mode batch, SQLite recrée
    # les tables (copie puis DROP), et un DROP avec les clés étrangères actives déclencherait les
    # ON DELETE CASCADE des tables filles
    connectable = create_engine(DATABASE_URL, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite ne sait pas modifier une colonne ou une contrainte : Alembic recrée la table
            render_as_batch=connection.dialect.name == "sqlite",
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
