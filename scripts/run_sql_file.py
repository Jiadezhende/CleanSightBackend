"""Run a SQL file using CLEANSIGHT_DB_* settings from an env file.

Usage:
    python scripts/run_sql_file.py scripts/sql/01_cleansight_schema.sql --env .env.dev
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import psycopg2


def load_env_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"env file not found: {path}")

    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required env: {name}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sql_file", type=Path)
    parser.add_argument("--env", dest="env_file", type=Path, default=Path(".env.dev"))
    args = parser.parse_args()

    load_env_file(args.env_file)

    sql_path = args.sql_file
    if not sql_path.exists():
        raise FileNotFoundError(f"sql file not found: {sql_path}")
    sql = sql_path.read_text(encoding="utf-8")

    conn = psycopg2.connect(
        host=require_env("CLEANSIGHT_DB_HOST"),
        port=int(require_env("CLEANSIGHT_DB_PORT")),
        dbname=require_env("CLEANSIGHT_DB_NAME"),
        user=require_env("CLEANSIGHT_DB_USER"),
        password=require_env("CLEANSIGHT_DB_PASSWORD"),
    )
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql)
    finally:
        conn.close()

    print(f"SQL executed successfully: {sql_path}")


if __name__ == "__main__":
    main()
