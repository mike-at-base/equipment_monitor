import os
import psycopg2
from psycopg2 import pool

_pool: pool.ThreadedConnectionPool | None = None

DSN = os.environ.get(
    "DATABASE_URL",
    "host=localhost port=5432 dbname=equipment user=monitor password=monitor",
)


def get_pool() -> pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = pool.ThreadedConnectionPool(minconn=1, maxconn=10, dsn=DSN)
    return _pool


class Conn:
    """Context manager that borrows a connection from the pool."""

    def __enter__(self) -> psycopg2.extensions.connection:
        self._conn = get_pool().getconn()
        return self._conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self._conn.rollback()
        else:
            self._conn.commit()
        get_pool().putconn(self._conn)
        return False
