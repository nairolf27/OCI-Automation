"""
Filename    : test_connection.py
Description : Verifies that the ORDS SQL endpoint is reachable and that the
              admin credentials are valid by running a trivial SELECT query.
              Run this first to confirm the network and auth setup is correct
              before executing any reconciliation.
Author      : Florian NOEL
Date        : 12/06/2025
"""

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
log = logging.getLogger("test_connection")


# ─────────────────────────────────────────────────────────────────────────────
#  ENVIRONMENT VARIABLES
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

ORDS_SQL_PATH = os.environ.get("ORDS_SQL_PATH", "/_/sql")
HTTP_TIMEOUT  = int(os.environ.get("HTTP_TIMEOUT", "30"))


# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

TEST_SQL = "SELECT 'ORDS_OK' AS status FROM dual"


# ─────────────────────────────────────────────────────────────────────────────
#  FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def build_endpoint(base_url: str, admin_user: str, sql_path: str) -> str:
    """Assemble the ORDS SQL endpoint URL for the given database."""
    return f"{base_url.rstrip('/')}/{admin_user.lower()}{sql_path}"


def test_database_connection(db_name: str, base_url: str, admin_user: str, password: str) -> bool:
    """
    Send a trivial SELECT to verify that ORDS is reachable, the credentials
    are accepted, and the SQL engine returns the expected result row.
    Returns True on success, False on any failure so the caller can report
    all databases rather than stopping at the first error.
    """
    endpoint = build_endpoint(base_url, admin_user, ORDS_SQL_PATH)
    log.info(f"  Endpoint : {endpoint}")
    log.info(f"  User     : {admin_user}")

    try:
        response = requests.post(
            url=endpoint,
            data=TEST_SQL,
            headers={"Content-Type": "application/sql"},
            auth=(admin_user, password),
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()

        rows = (
            body.get("items", [{}])[0]
            .get("resultSet", {})
            .get("items", [])
        )
        if rows and list(rows[0].values())[0] == "ORDS_OK":
            log.info(f"  Result   : OK — SQL engine responded correctly\n")
            return True

        log.warning(f"  Result   : Unexpected response body: {body}\n")
        return False

    except requests.exceptions.ConnectionError:
        log.error(f"  Result   : Connection refused — check ORDS_BASE_URL\n")
        return False
    except requests.exceptions.HTTPError as exc:
        log.error(f"  Result   : HTTP {exc.response.status_code} — check credentials\n")
        return False
    except requests.exceptions.Timeout:
        log.error(f"  Result   : Timeout after {HTTP_TIMEOUT}s\n")
        return False


def load_databases() -> dict:
    """
    Load the databases block from atp_users.json, resolving credentials
    from environment variables using the <DB_NAME>_ORDS_ADMIN_PASSWORD pattern.
    """
    import json
    config_path = os.path.join(os.path.dirname(__file__), "..", "atp_users.json")
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    databases = {}
    for db_name, db_config in config.get("databases", {}).items():
        password = os.environ.get(f"{db_name}_ORDS_ADMIN_PASSWORD", "")
        databases[db_name] = {
            "ords_base_url":   db_config.get("ords_base_url", ""),
            "ords_admin_user": db_config.get("ords_admin_user", "ADMIN"),
            "password":        password,
        }
    return databases


def main() -> None:
    """Test connectivity for every database declared in atp_users.json."""
    log.info("╔══════════════════════════════════════╗")
    log.info("║        ORDS Connection Test          ║")
    log.info("╚══════════════════════════════════════╝\n")

    databases = load_databases()
    results   = {}

    for db_name, db in databases.items():
        log.info(f"── {db_name} {'─' * (34 - len(db_name))}")
        if not db["ords_base_url"]:
            log.error("  Missing ords_base_url in config. Skipping.\n")
            results[db_name] = False
            continue
        if not db["password"]:
            log.error(f"  Missing {db_name}_ORDS_ADMIN_PASSWORD in .env. Skipping.\n")
            results[db_name] = False
            continue

        results[db_name] = test_database_connection(
            db_name,
            db["ords_base_url"],
            db["ords_admin_user"],
            db["password"],
        )

    log.info("── Summary " + "─" * 27)
    all_ok = True
    for db_name, success in results.items():
        status = "[OK]" if success else "[KO]"
        log.info(f"  {status}  {db_name}")
        if not success:
            all_ok = False

    log.info("")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()