from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

type Connection = AsyncConnection[TupleRow]
type Pool = AsyncConnectionPool[Connection]


def create_pool(url: str, *, min_size: int = 1, max_size: int = 10) -> Pool:
    return AsyncConnectionPool(url, min_size=min_size, max_size=max_size, open=False)
