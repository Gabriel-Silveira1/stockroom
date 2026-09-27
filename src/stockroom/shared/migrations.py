from importlib.resources import files

from stockroom.shared.db import Connection

_CREATE_LEDGER = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    service    text        NOT NULL,
    version    text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (service, version)
)
"""


async def apply_migrations(conn: Connection, *, service: str, package: str) -> list[str]:
    """Apply pending `*.sql` files from `package`, in name order, in one transaction.

    The advisory lock makes it safe for several replicas to start at the same time.
    """
    scripts = sorted(
        (entry for entry in files(package).iterdir() if entry.name.endswith(".sql")),
        key=lambda entry: entry.name,
    )
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('schema_migrations'))")
        await conn.execute(_CREATE_LEDGER)
        cursor = await conn.execute(
            "SELECT version FROM public.schema_migrations WHERE service = %s", (service,)
        )
        applied = {version for (version,) in await cursor.fetchall()}
        pending = [script for script in scripts if script.name not in applied]
        for script in pending:
            await conn.execute(script.read_bytes())
            await conn.execute(
                "INSERT INTO public.schema_migrations (service, version) VALUES (%s, %s)",
                (service, script.name),
            )
    return [script.name for script in pending]
