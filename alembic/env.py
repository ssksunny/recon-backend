"""
Alembic migration environment.

Deliberately does NOT read sqlalchemy.url from alembic.ini — the database
URL comes from the same place the running app gets it (app.core.config.settings,
i.e. the DATABASE_URL env var), so migrations always run against whatever
database the app itself is configured for. This is also why alembic.ini's
sqlalchemy.url line is left blank: filling it in would silently disagree
with DATABASE_URL in any environment where they differ.
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Import every model module so Base.metadata is fully populated before
# autogenerate compares it against the database — a model that's never
# imported never registers itself with Base, and its table would look like
# a spurious DROP to `alembic revision --autogenerate`.
from app.models.database import Base
from app.models import models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_database_url() -> str:
    from app.core.config import settings

    return settings.database_url


def run_migrations_offline() -> None:
    """
    Emits SQL to stdout instead of executing against a live database — used
    for generating a .sql script to hand to a DBA/CI step rather than
    letting Alembic connect directly (`alembic upgrade head --sql`).
    """
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Catch column type changes (e.g. String(50) -> String(100)) in
            # autogenerate, not just added/removed tables and columns.
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
