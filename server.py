"""
MCP server exposing the User Admin `users` table (PostgreSQL) as read-only tools.

Transport: stdio. The NestJS backend starts this script and talks MCP over stdin/stdout,
so nothing may be printed to stdout except protocol messages (logging goes to stderr).

Database settings come from environment variables (DB_HOST, DB_PORT, DB_USER, DB_PASSWORD,
DB_NAME); anything not set is read from ../Nest_Js_Project/.env.

Every query runs in a READ ONLY transaction with a statement timeout, so a tool call can
never change data or hang the database.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal

import psycopg
from dotenv import dotenv_values
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="[mcp-users] %(levelname)s %(message)s")
log = logging.getLogger("mcp-users")

MAX_LIMIT = 100
STATEMENT_TIMEOUT_MS = 5_000


# ---------- configuration ----------

def _db_settings() -> dict[str, str | int]:
    fallback_file = Path(__file__).resolve().parent.parent / "Nest_Js_Project" / ".env"
    fallback = dotenv_values(fallback_file) if fallback_file.exists() else {}

    def setting(key: str, default: str | None = None) -> str | None:
        return os.environ.get(key) or fallback.get(key) or default

    return {
        "host": setting("DB_HOST", "localhost"),
        "port": int(setting("DB_PORT", "5432")),
        "user": setting("DB_USER", "postgres"),
        "password": setting("DB_PASSWORD", "8991"),
        "dbname": setting("DB_NAME", "postgres"),
        "connect_timeout": 5,
    }


DB = _db_settings()


def _query(sql: str, params: dict | None = None) -> list[dict]:
    """Run one read-only query and return rows as dicts."""
    try:
        with psycopg.connect(**DB, row_factory=dict_row) as conn:
            conn.read_only = True
            with conn.transaction():
                conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
                return conn.execute(sql, params or {}).fetchall()
    except psycopg.OperationalError as err:
        log.error("database unavailable: %s", err)
        raise ToolError("The database is not reachable right now.") from err


# ---------- result models (become the tools' JSON output schema) ----------

class User(BaseModel):
    id: int
    name: str
    email: str
    age: int | None
    created_at: datetime


class UserList(BaseModel):
    total: int = Field(description="Users matching the search, across all pages")
    count: int = Field(description="Users returned in this response")
    offset: int
    users: list[User]


class UserCount(BaseModel):
    total: int


class UserStats(BaseModel):
    total: int
    with_age: int
    average_age: float | None
    youngest: User | None
    oldest: User | None
    newest: User | None


USER_COLUMNS = "id, name, email, age, created_at"
SORTS = {"id": "id ASC", "name": "lower(name) ASC, id ASC", "age": "age ASC NULLS LAST, id ASC", "newest": "created_at DESC, id DESC"}


def _search_filter(search: str | None) -> tuple[str, dict]:
    if not search or not search.strip():
        return "", {}
    return "WHERE name ILIKE %(pattern)s OR email ILIKE %(pattern)s", {"pattern": f"%{search.strip()}%"}


# ---------- server + tools ----------

server = MCPServer(
    name="user-admin-users",
    title="User Admin users",
    instructions="Read-only access to the users table of the User Admin app. Use these tools for any question about users.",
    version="1.0.0",
)


@server.tool(title="List users")
def list_users(
    limit: int = 20,
    offset: int = 0,
    search: str | None = None,
    sort: Literal["id", "name", "age", "newest"] = "id",
) -> UserList:
    """List users from the database. Use for "show/list all users" or "find users named X".

    Args:
        limit: How many users to return (1-100, default 20).
        offset: How many users to skip, for paging.
        search: Optional text to match in the name or email (case-insensitive).
        sort: Order: "id", "name", "age" or "newest".
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    where, params = _search_filter(search)
    order = SORTS.get(sort, SORTS["id"])
    total = _query(f"SELECT count(*) AS n FROM users {where}", params)[0]["n"]
    rows = _query(
        f"SELECT {USER_COLUMNS} FROM users {where} ORDER BY {order} LIMIT %(limit)s OFFSET %(offset)s",
        {**params, "limit": limit, "offset": offset},
    )
    log.info("list_users limit=%s offset=%s search=%r -> %s of %s", limit, offset, search, len(rows), total)
    return UserList(total=total, count=len(rows), offset=offset, users=[User(**r) for r in rows])


@server.tool(title="Get user")
def get_user(user_id: int) -> User:
    """Get one user by id.

    Args:
        user_id: The user's id.
    """
    rows = _query(f"SELECT {USER_COLUMNS} FROM users WHERE id = %(id)s", {"id": int(user_id)})
    if not rows:
        raise ToolError(f"User #{user_id} not found.")
    return User(**rows[0])


@server.tool(title="Count users")
def count_users(search: str | None = None) -> UserCount:
    """Count users, optionally only those whose name or email matches `search`.

    Args:
        search: Optional text to match in the name or email (case-insensitive).
    """
    where, params = _search_filter(search)
    return UserCount(total=_query(f"SELECT count(*) AS n FROM users {where}", params)[0]["n"])


@server.tool(title="User statistics")
def user_stats() -> UserStats:
    """Summary of all users: total, average age, youngest, oldest and newest user."""
    summary = _query("SELECT count(*) AS total, count(age) AS with_age, round(avg(age)::numeric, 1) AS avg_age FROM users")[0]

    def one(order: str, where: str = "") -> User | None:
        rows = _query(f"SELECT {USER_COLUMNS} FROM users {where} ORDER BY {order} LIMIT 1")
        return User(**rows[0]) if rows else None

    return UserStats(
        total=summary["total"],
        with_age=summary["with_age"],
        average_age=float(summary["avg_age"]) if summary["avg_age"] is not None else None,
        youngest=one("age ASC, id ASC", "WHERE age IS NOT NULL"),
        oldest=one("age DESC, id ASC", "WHERE age IS NOT NULL"),
        newest=one("created_at DESC, id DESC"),
    )


if __name__ == "__main__":
    log.info("starting (stdio), database %s@%s:%s/%s", DB["user"], DB["host"], DB["port"], DB["dbname"])
    server.run()
