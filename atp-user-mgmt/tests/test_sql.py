"""
Filename    : test_sql.py
Description : Sends an arbitrary SQL statement to a target ATP database via
              ORDS and prints the raw response. Useful for manually testing
              specific DDL/DML statements (CREATE USER, GRANT, SELECT …)
              before the reconciler runs them, or for ad-hoc inspection.
Author      : Florian NOEL
Date        : 12/06/2025
"""

import json
import logging
import os
import sys

import requests
from dotenv import load_dotenv


# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING SETUP
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("test_sql")


# ─────────────────────────────────────────────────────────────────────────────
#  ENVIRONMENT VARIABLES
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

ORDS_SQL_PATH = os.environ.get("ORDS_SQL_PATH", "/_/sql")
HTTP_TIMEOUT  = int(os.environ.get("HTTP_TIMEOUT", "30"))


# ─────────────────────────────────────────────────────────────────────────────
#  FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def build_endpoint(base_url: str, admin_user: str, sql_path: str) -> str:
    """Assemble the ORDS SQL endpoint URL for the given database."""
    return f"{base_url.rstrip('/')}/{admin_user.lower()}{sql_path}"


def load_database(db_name: str) -> dict | None:
    """
    Return the connection info (url, admin user, password) for a given
    database name, reading credentials from env and config from JSON.
    """
    config_path = os.path.join(os.path.dirname(__file__), "..", "atp_users.json")
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    db_config = config.get("databases", {}).get(db_name)
    if not db_config:
        return None

    return {
        "ords_base_url":   db_config.get("ords_base_url", ""),
        "ords_admin_user": db_config.get("ords_admin_user", "ADMIN"),
        "password":        os.environ.get(f"{db_name}_ORDS_ADMIN_PASSWORD", ""),
    }


def send_sql(endpoint: str, sql: str, admin_user: str, password: str) -> dict | None:
    """
    POST the SQL statement to ORDS and return the parsed JSON response,
    or None on HTTP/network failure.
    """
    try:
        response = requests.post(
            url=endpoint,
            data=sql,
            headers={"Content-Type": "application/sql"},
            auth=(admin_user, password),
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as exc:
        log.error(f"HTTP {exc.response.status_code}: {exc.response.text[:500]}")
        return None
    except requests.exceptions.RequestException as exc:
        log.error(f"Request failed: {exc}")
        return None


def display_response(body: dict) -> None:
    """
    Render the ORDS response in a readable format: tabular for SELECT
    results, status line for DDL (CREATE, GRANT, DROP …).
    """
    items = body.get("items", [])
    if not items:
        log.info("  (empty response)")
        return

    for item in items:
        result_set = item.get("resultSet", {})
        rows       = result_set.get("items", [])

        if rows:
            # SELECT — print as table
            headers = list(rows[0].keys())
            col_widths = [
                max(len(h), max((len(str(row.get(h, ""))) for row in rows), default=0))
                for h in headers
            ]
            header_line = "  " + "  ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
            separator   = "  " + "  ".join("─" * w for w in col_widths)
            log.info(header_line)
            log.info(separator)
            for row in rows:
                log.info("  " + "  ".join(str(row.get(h, "")).ljust(col_widths[i]) for i, h in enumerate(headers)))
            log.info(f"\n  {len(rows)} row(s) returned")
        else:
            # DDL / DML — show status
            response_val = item.get("response", [])
            if isinstance(response_val, list) and response_val:
                for line in response_val:
                    log.info(f"  {line}")
            else:
                log.info("  Statement executed (no result set returned)")

        log.info("")


def main() -> None:
    """
    Send an arbitrary SQL statement to a target database and print the result.

    Usage:
        python test_sql.py <DB_NAME> "<SQL>"

    Examples:
        python test_sql.py PROD "SELECT username FROM dba_users WHERE oracle_maintained = 'N'"
        python test_sql.py DEV  "GRANT CONNECT TO \\"MY_USER\\""
        python test_sql.py PROD "SELECT * FROM dba_role_privs WHERE grantee = 'APP_USER_1'"
    """
    log.info("╔══════════════════════════════════════╗")
    log.info("║         ATP — SQL Test Runner        ║")
    log.info("╚══════════════════════════════════════╝\n")

    if len(sys.argv) < 3:
        log.error('Usage: python test_sql.py <DB_NAME> "<SQL>"')
        log.error('Example: python test_sql.py PROD "SELECT username FROM dba_users WHERE oracle_maintained = \'N\'"')
        sys.exit(1)

    db_name = sys.argv[1].upper()
    sql     = sys.argv[2]

    db = load_database(db_name)
    if not db:
        log.error(f"Database '{db_name}' not found in atp_users.json.")
        sys.exit(1)
    if not db["password"]:
        log.error(f"Missing {db_name}_ORDS_ADMIN_PASSWORD in .env.")
        sys.exit(1)

    endpoint = build_endpoint(db["ords_base_url"], db["ords_admin_user"], ORDS_SQL_PATH)

    log.info(f"  Database : {db_name}")
    log.info(f"  Endpoint : {endpoint}")
    log.info(f"  SQL      : {sql}\n")

    body = send_sql(endpoint, sql, db["ords_admin_user"], db["password"])
    if body:
        display_response(body)
    else:
        log.error("No response received.")
        sys.exit(1)


if __name__ == "__main__":
    main()