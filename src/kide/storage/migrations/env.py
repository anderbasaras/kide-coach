"""Alembic environment.

The database URL comes from the Alembic config when set (``kide db upgrade``,
tests) and otherwise from the app settings (``DATABASE_URL`` / ``DATA_DIR``).
"""

from alembic import context

from kide.config import get_settings
from kide.storage.db import make_engine
from kide.storage.models import Base, UTCDateTime

config = context.config
target_metadata = Base.metadata


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Render app-specific column types as plain SQLAlchemy ones.

    Migrations must not import application code that may change later.
    """
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime()"
    return False


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,  # SQLite cannot ALTER most things in place
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
