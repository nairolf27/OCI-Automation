"""
Filename    : test_list_users.py
Description : Queries a target ATP database via ORDS and prints all
              non-Oracle-internal users along with their currently granted
              roles. Useful to verify the live state before or after a
              reconciliation run, and to manually inspect what the reconciler
              would read as its current_state.
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
log = logging.getLogger("test_list_users")


# ─────────────────────────────────────────────────────────────────────────────
#  ENVIRONMENT VARIABLES
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

ORDS_SQL_PATH = os.environ.get("ORDS_SQL_PATH", "/_/sql")
HTTP_TIMEOUT  = int(os.environ.get("HTTP_TIMEOUT", "30"))


# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

SQL_LIST_USERS = (
    "SELECT username, account_status "
    "FROM dba_users "
    "WHERE oracle_maintained = 'N' "
    "ORDER BY username"
)

SQL_LIST_ROLES = (
    "SELECT grantee, granted_role "
    "FROM dba_role_privs "
    "WHERE grantee IN (SELECT username FROM dba_users WHERE oracle_maintained = 'N') "
    "ORDER BY grantee, granted_role"
)


# ─────────────────────────────────────────────────────────────────────────────
#  FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def build_endpoint(base_url: str, admin_user: str, sql_path: str) -> str:
    """Assemble the ORDS SQL endpoint URL for the given database."""
    return f"{base_url.rstrip('/')}/{admin_user.lower()}{sql_path}"


def run_query(endpoint: str, sql: str, admin_user: str, password: str) -> list:
    """
    Execute a SELECT via ORDS and return the result rows as a list of
    value lists, or an empty list if the request fails.
    """
    response = requests.post(
        url=endpoint,
        data=sql,
        headers={"Content-Type": "application/sql"},
        auth=(admin_user, password),
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    items = body.get("items", [{}])[0].get("resultSet", {}).get("items", [])
    return [list(row.values()) for row in items]


def list_users_for_database(
    db_name: str,
    base_url: str,
    admin_user: str,
    password: str,
) -> None:
    """
    Fetch and display all non-system users with their roles for one database,
    mimicking exactly what fetch_current_users() in the reconciler reads.
    """
    endpoint = build_endpoint(base_url, admin_user, ORDS_SQL_PATH)

    try:
        users_rows = run_query(endpoint, SQL_LIST_USERS, admin_user, password)
        roles_rows = run_query(endpoint, SQL_LIST_ROLES, admin_user, password)
    except requests.exceptions.HTTPError as exc:
        log.error(f"  HTTP {exc.response.status_code} — check credentials or endpoint")
        return
    except requests.exceptions.RequestException as exc:
        log.error(f"  Request failed: {exc}")
        return

    # Build roles index: { username -> [role, ...] }
    roles_index: dict = {}
    for row in roles_rows:
        username = row[0].upper()
        roles_index.setdefault(username, []).append(row[1])

    if not users_rows:
        log.info("  (no non-system users found)\n")
        return

    for row in users_rows:
        username = row[0].upper()
        status   = row[1]
        roles    = roles_index.get(username, [])
        roles_str = ", ".join(sorted(roles)) if roles else "—"
        log.info(f"  {username:<30} [{status:<8}]  roles: {roles_str}")

    log.info(f"\n  Total: {len(users_rows)} user(s)\n")


def load_databases() -> dict:
    """
    Load database configs from atp_users.json and resolve passwords from env.
    """
    config_path = os.path.join(os.path.dirname(__file__), "..", "atp_users.json")
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    databases = {}
    for db_name, db_config in config.get("databases", {}).items():
        databases[db_name] = {
            "ords_base_url":   db_config.get("ords_base_url", ""),
            "ords_admin_user": db_config.get("ords_admin_user", "ADMIN"),
            "password":        os.environ.get(f"{db_name}_ORDS_ADMIN_PASSWORD", ""),
        }
    return databases


def main() -> None:
    """
    List users and roles for a specific database (passed as CLI argument)
    or for all databases if no argument is given.

    Usage:
        python test_list_users.py          # all databases
        python test_list_users.py PROD     # PROD only
    """
    log.info("╔══════════════════════════════════════╗")
    log.info("║         ATP — List Users             ║")
    log.info("╚══════════════════════════════════════╝\n")

    databases  = load_databases()
    target     = sys.argv[1].upper() if len(sys.argv) > 1 else None

    if target and target not in databases:
        log.error(f"Database '{target}' not found in atp_users.json.")
        log.error(f"Available: {', '.join(databases.keys())}")
        sys.exit(1)

    selected = {target: databases[target]} if target else databases

    for db_name, db in selected.items():
        log.info(f"── {db_name} {'─' * (34 - len(db_name))}")
        if not db["password"]:
            log.error(f"  Missing {db_name}_ORDS_ADMIN_PASSWORD in .env. Skipping.\n")
            continue
        list_users_for_database(db_name, db["ords_base_url"], db["ords_admin_user"], db["password"])


if __name__ == "__main__":
    main()