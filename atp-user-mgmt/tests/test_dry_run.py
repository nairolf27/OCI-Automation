"""
Filename    : test_dry_run.py
Description : Performs a full reconciliation dry-run: loads the desired state
              from atp_users.json, fetches the current state from every ATP
              database, computes the verified diff, and displays the planned
              actions — without applying any change and without prompting for
              confirmation. Identical output to the real reconciler's planning
              phase, safe to run at any time.
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
log = logging.getLogger("test_dry_run")


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


def run_query(endpoint: str, sql: str, admin_user: str, password: str) -> list:
    """Execute a SELECT via ORDS and return rows as value lists."""
    response = requests.post(
        url=endpoint,
        data=sql,
        headers={"Content-Type": "application/sql"},
        auth=(admin_user, password),
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    body  = response.json()
    items = body.get("items", [{}])[0].get("resultSet", {}).get("items", [])
    return [list(row.values()) for row in items]


def fetch_current_state(endpoint: str, admin_user: str, password: str) -> dict:
    """
    Mirror of the main reconciler's fetch_current_users: returns a dict
    keyed by username with their currently granted roles.
    """
    sql_users = (
        "SELECT username FROM dba_users "
        "WHERE oracle_maintained = 'N' ORDER BY username"
    )
    sql_roles = (
        "SELECT grantee, granted_role FROM dba_role_privs "
        "WHERE grantee IN (SELECT username FROM dba_users WHERE oracle_maintained = 'N') "
        "ORDER BY grantee, granted_role"
    )
    current: dict = {}
    for row in run_query(endpoint, sql_users, admin_user, password):
        current[row[0].upper()] = {"roles": []}
    for row in run_query(endpoint, sql_roles, admin_user, password):
        username = row[0].upper()
        if username in current:
            current[username]["roles"].append(row[1])
    return current


def resolve_user_roles(user_config: dict, profiles: dict) -> list:
    """Merge direct roles with profile roles, deduplicating."""
    profile_name  = user_config.get("profile")
    profile_roles = profiles.get(profile_name, {}).get("roles", []) if profile_name else []
    merged: list  = list(user_config.get("roles", []))
    for role in profile_roles:
        if role not in merged:
            merged.append(role)
    return merged


def config_to_desired_state(db_config: dict) -> dict:
    """Extract the desired state for present users from a database config block."""
    profiles = db_config.get("profiles", {})
    desired: dict = {}
    for user in db_config.get("users", []):
        if user.get("state") != "present":
            continue
        username = user["username"].upper()
        desired[username] = {
            "password": user.get("password", ""),
            "roles":    resolve_user_roles(user, profiles),
        }
    return desired


def get_user_config(db_config: dict, username: str) -> dict | None:
    """Return the raw config dict for a username (case-insensitive), or None."""
    for user in db_config.get("users", []):
        if user["username"].upper() == username.upper():
            return user
    return None


def plan_changes(desired: dict, current: dict) -> dict:
    """Compute the full diff between desired and current state."""
    changes: dict = {
        "create_user": [],
        "drop_user":   [],
        "grant_role":  {},
        "revoke_role": {},
    }
    desired_set = set(desired.keys())
    current_set = set(current.keys())
    changes["create_user"] = sorted(desired_set - current_set)
    changes["drop_user"]   = sorted(current_set - desired_set)

    for username in desired_set & current_set:
        to_grant  = set(desired[username]["roles"]) - set(current[username]["roles"])
        to_revoke = set(current[username]["roles"]) - set(desired[username]["roles"])
        if to_grant:
            changes["grant_role"][username]  = to_grant
        if to_revoke:
            changes["revoke_role"][username] = to_revoke

    for username in changes["create_user"]:
        roles = desired.get(username, {}).get("roles", [])
        if roles:
            changes["grant_role"][username] = set(roles)

    return changes


def verify_planned_changes(planned: dict, db_config: dict) -> dict:
    """Filter out drops for users not explicitly marked absent."""
    verified = {
        key: (value.copy() if isinstance(value, (list, set)) else dict(value))
        for key, value in planned.items()
    }
    verified["create_user"] = [
        u for u in planned["create_user"]
        if (cfg := get_user_config(db_config, u)) and cfg.get("state") == "present"
    ]
    verified_drop = []
    for username in planned["drop_user"]:
        cfg = get_user_config(db_config, username)
        if cfg is not None and cfg.get("state") == "absent":
            verified_drop.append(username)
        else:
            log.info(f"  [SKIP] '{username}' not marked 'absent' — would not be dropped")
    verified["drop_user"] = verified_drop
    return verified


def display_changes(db_name: str, changes: dict) -> int:
    """
    Print the planned actions for one database and return the action count
    so the caller can decide whether anything needs to happen.
    """
    log.info(f"── {db_name} {'─' * (34 - len(db_name))}")
    count = 0

    for username in changes["create_user"]:
        log.info(f"  [+] CREATE USER  {username}")
        count += 1
    for username in changes["drop_user"]:
        log.info(f"  [-] DROP USER    {username}")
        count += 1
    for username, roles in changes["grant_role"].items():
        log.info(f"  [>] GRANT ROLE   {username}  →  {', '.join(sorted(roles))}")
        count += 1
    for username, roles in changes["revoke_role"].items():
        log.info(f"  [<] REVOKE ROLE  {username}  ←  {', '.join(sorted(roles))}")
        count += 1

    if count == 0:
        log.info("  (nothing to do — already in desired state)")

    log.info("")
    return count


def main() -> None:
    """
    Run a full dry-run for all databases and display what the reconciler
    would do, without applying any change.

    Usage:
        python test_dry_run.py          # all databases
        python test_dry_run.py PROD     # PROD only
    """
    log.info("╔══════════════════════════════════════╗")
    log.info("║     ATP — Reconciliation Dry-Run     ║")
    log.info("╚══════════════════════════════════════╝\n")

    config_path = os.path.join(os.path.dirname(__file__), "..", "atp_users.json")
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    databases = config.get("databases", {})
    target    = sys.argv[1].upper() if len(sys.argv) > 1 else None

    if target and target not in databases:
        log.error(f"Database '{target}' not found in atp_users.json.")
        sys.exit(1)

    selected     = {target: databases[target]} if target else databases
    total_actions = 0

    for db_name, db_config in selected.items():
        password = os.environ.get(f"{db_name}_ORDS_ADMIN_PASSWORD", "")
        if not password:
            log.error(f"  Missing {db_name}_ORDS_ADMIN_PASSWORD in .env. Skipping.\n")
            continue

        admin_user = db_config.get("ords_admin_user", "ADMIN")
        endpoint   = build_endpoint(db_config.get("ords_base_url", ""), admin_user, ORDS_SQL_PATH)

        try:
            current_state = fetch_current_state(endpoint, admin_user, password)
        except requests.exceptions.RequestException as exc:
            log.error(f"  [{db_name}] Failed to fetch current state: {exc}\n")
            continue

        desired_state = config_to_desired_state(db_config)
        planned       = plan_changes(desired_state, current_state)
        verified      = verify_planned_changes(planned, db_config)
        total_actions += display_changes(db_name, verified)

    if total_actions == 0:
        log.info("All databases are already in the desired state.")
    else:
        log.info(f"{total_actions} action(s) would be applied. Run atp_reconcile.py to execute.")


if __name__ == "__main__":
    main()