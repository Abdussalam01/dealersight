"""Postgres connection helpers. Run `python -m backend.db init` to create tables."""

import sys

import psycopg
from psycopg.rows import dict_row

from backend import config

SCHEMA_PATH = config.ROOT / "db" / "schema.sql"


def connect(url=None):
    return psycopg.connect(url or config.DATABASE_URL, row_factory=dict_row, autocommit=True)


def init_schema(conn):
    conn.execute(SCHEMA_PATH.read_text())


if __name__ == "__main__" and sys.argv[1:] == ["init"]:
    with connect() as conn:
        init_schema(conn)
    print("schema applied")
