"""
Filename    : atp_reconcile.py
Description : Reconciles Oracle ATP database users across multiple instances
              via ORDS SQL HTTP requests. Manages only role grants and revokes.
              Computes the diff between a desired JSON configuration and the
              current state of each database, then applies creates, drops, role
              grants and revokes after a single operator confirmation.
Author      : Florian NOEL
Date        : 12/06/2025
"""

import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Optional

import requests
from dotenv import load_dotenv


# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING SETUP
# ─────────────────────────────────────────────────────────────────────────────

def setup_logger(log_dir: str) -> logging.Logger:
    """
    Configure a dual-handler logger: human-readable console output and a
    structured timestamped file trace for audit purposes.
    """
    os.makedirs(log_dir, exist_ok=True)
    log_filename = os.path.join(
        log_dir,
        f"atp_reconcile_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )

    logger = logging.getLogger("atp_reconcile")
    logger.setLevel(logging.DEBUG)

    class ConsoleFormatter(logging.Formatter):
        """Strip noise from INFO, decorate WARNING/ERROR for the operator."""

        def format(self, record: logging.LogRecord) -> str:
            if record.levelno == logging.INFO:
                return record.getMessage()
            if record.levelno == logging.WARNING:
                return f"[WARNING] {record.getMessage()}"
            if record.levelno == logging.ERROR:
                return f"[ERROR]   {record.getMessage()}"
            if record.levelno == logging.DEBUG:
                return f"   {record.getMessage()}"
            return record.getMessage()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(ConsoleFormatter())

    file_handler = logging.FileHandler(log_filename, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.info(f"Log file: {log_filename}\n")
    return logger


# ─────────────────────────────────────────────────────────────────────────────
#  ENVIRONMENT VARIABLES
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()

ORDS_SQL_PATH      = os.environ.get("ORDS_SQL_PATH", "/_/sql")
ATP_USERS_FILE     = os.environ.get("ATP_USERS_FILE", "atp_users.json")
REQUIRE_VALIDATION = os.environ.get("REQUIRE_VALIDATION", "true").lower() == "true"
VALIDATION_KEYWORD = os.environ.get("VALIDATION_KEYWORD", "OK")
LOG_DIR            = os.environ.get("LOG_DIR", "logs")
HTTP_TIMEOUT       = int(os.environ.get("HTTP_TIMEOUT", "30"))
REQUEST_DELAY      = float(os.environ.get("REQUEST_DELAY", "0.3"))


# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

EXIT_CONFIG_ERROR = 1
EXIT_CANCELED     = 2


# ─────────────────────────────────────────────────────────────────────────────
#  ORDS / HTTP HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def build_sql_endpoint(base_url: str, admin_user: str, sql_path: str) -> str:
    """
    Assemble the fully-qualified ORDS SQL endpoint URL so it can be
    reused across every request without repeating string manipulation.
    """
    return f"{base_url.rstrip('/')}/{admin_user.lower()}{sql_path}"


def execute_sql(
    endpoint: str,
    sql_statement: str,
    admin_user: str,
    admin_password: str,
    timeout: int,
    logger: logging.Logger,
) -> Optional[dict]:
    """
    Send a single SQL statement to the ORDS /_/sql endpoint via HTTP POST
    and return the parsed JSON response body, or None on failure.

    ORDS expects the body as plain text (the SQL statement itself) with
    Content-Type: application/sql.
    """
    logger.debug(f"SQL >> {sql_statement.strip()}")
    try:
        response = requests.post(
            url=endpoint,
            data=sql_statement,
            headers={"Content-Type": "application/sql"},
            auth=(admin_user, admin_password),
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as exc:
        logger.error(f"HTTP error executing SQL: {exc} — body: {exc.response.text[:500]}")
        return None
    except requests.exceptions.RequestException as exc:
        logger.error(f"Request failed: {exc}")
        return None


def _extract_rows(ords_response: dict) -> list:
    """
    Navigate the ORDS JSON envelope to extract the data rows from the first
    result set, regardless of column count or name.
    """
    try:
        items = ords_response.get("items", [])
        if not items:
            return []
        result_set = items[0]
        rows = result_set.get("resultSet", {}).get("items", [])
        return [list(row.values()) for row in rows]
    except (KeyError, IndexError, TypeError):
        return []


def fetch_current_users(
    endpoint: str,
    admin_user: str,
    admin_password: str,
    timeout: int,
    db_name: str,
    logger: logging.Logger,
) -> dict:
    """
    Query DBA_USERS and DBA_ROLE_PRIVS for all non-Oracle-internal accounts
    and return a dict keyed by username with their currently granted roles.

    Querying only the two views needed keeps the footprint minimal and avoids
    fetching privilege types that are out of scope for this reconciler.
    """
    logger.info(f"[{db_name}] Fetching current users …")

    user_sql = (
        "SELECT username FROM dba_users "
        "WHERE oracle_maintained = 'N' "
        "ORDER BY username"
    )
    role_sql = (
        "SELECT grantee, granted_role FROM dba_role_privs "
        "WHERE grantee IN (SELECT username FROM dba_users WHERE oracle_maintained = 'N') "
        "ORDER BY grantee, granted_role"
    )

    users_data = execute_sql(endpoint, user_sql, admin_user, admin_password, timeout, logger)
    roles_data = execute_sql(endpoint, role_sql, admin_user, admin_password, timeout, logger)

    current: dict = {}

    if users_data:
        for row in _extract_rows(users_data):
            current[row[0].upper()] = {"roles": []}

    if roles_data:
        for row in _extract_rows(roles_data):
            username = row[0].upper()
            if username in current:
                current[username]["roles"].append(row[1])

    logger.info(f"  Fetching current state … ({len(current)} user(s) found)\n")
    return current


# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG PARSING
# ─────────────────────────────────────────────────────────────────────────────

def resolve_user_roles(user_config: dict, profiles: dict) -> list:
    """
    Merge a user's direct roles with those inherited from their declared
    profile, deduplicating across both sources to avoid redundant SQL.
    """
    profile_name = user_config.get("profile")
    profile_roles = profiles.get(profile_name, {}).get("roles", []) if profile_name else []

    merged: list = list(user_config.get("roles", []))
    for role in profile_roles:
        if role not in merged:
            merged.append(role)

    return merged


def config_to_desired_state(db_config: dict) -> dict:
    """
    Transform a single database's config block into a normalised desired-state
    dict keyed by uppercased username, only for users marked as 'present'.
    """
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


def get_user_config(db_config: dict, username: str) -> Optional[dict]:
    """Return the raw config dict for a username (case-insensitive), or None."""
    for user in db_config.get("users", []):
        if user["username"].upper() == username.upper():
            return user
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  PLANNING
# ─────────────────────────────────────────────────────────────────────────────

def plan_changes(desired: dict, current: dict) -> dict:
    """
    Compute the full diff between desired and current state for one database,
    covering user lifecycle (create / drop) and role management independently.
    """
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

    # Pre-populate role grants for users being created
    for username in changes["create_user"]:
        roles = desired.get(username, {}).get("roles", [])
        if roles:
            changes["grant_role"][username] = set(roles)

    return changes


def verify_planned_changes(planned: dict, db_config: dict) -> dict:
    """
    Guard against unintended destructive operations: only drop users that are
    explicitly declared 'absent' in the config; silently skip all others.
    """
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
            logging.getLogger("atp_reconcile").debug(
                f"Skipping drop of '{username}' — not explicitly marked 'absent' in config."
            )
    verified["drop_user"] = verified_drop

    return verified


# ─────────────────────────────────────────────────────────────────────────────
#  DISPLAY & VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def present_actions(all_verified_changes: dict, logger: logging.Logger) -> None:
    """
    Render a numbered summary of every planned action across all databases so
    the operator can review the full scope before confirming or canceling.
    """
    logger.info("┌─────────────────────────────────────┐")
    logger.info("│           PLANNED ACTIONS           │")
    logger.info("└─────────────────────────────────────┘")

    action_number = 0

    for db_name, changes in all_verified_changes.items():
        logger.info(f"\n  Database: {db_name}")

        for username in changes["create_user"]:
            action_number += 1
            logger.info(f"  {action_number:>2}. [+] CREATE USER  {username}")

        for username in changes["drop_user"]:
            action_number += 1
            logger.info(f"  {action_number:>2}. [-] DROP USER    {username}")

        for username, roles in changes["grant_role"].items():
            action_number += 1
            logger.info(f"  {action_number:>2}. [>] GRANT ROLE   {username}  →  {', '.join(sorted(roles))}")

        for username, roles in changes["revoke_role"].items():
            action_number += 1
            logger.info(f"  {action_number:>2}. [<] REVOKE ROLE  {username}  ←  {', '.join(sorted(roles))}")

    if action_number == 0:
        logger.info("\n  [OK] Everything is already in the desired state. No changes needed.")

    logger.info("")


def request_operator_confirmation(keyword: str) -> bool:
    """
    Block execution and require the operator to type the confirmation keyword,
    preventing accidental application of destructive changes.
    """
    if not REQUIRE_VALIDATION:
        return True

    print("┌─────────────────────────────────────┐")
    print("│           VALIDATION                │")
    print("└─────────────────────────────────────┘")
    user_input = input(f"\n  Type '{keyword}' to confirm, anything else to cancel.\n  > ")
    print()
    return user_input.strip().lower() == keyword.lower()


# ─────────────────────────────────────────────────────────────────────────────
#  SQL STATEMENT BUILDERS
# ─────────────────────────────────────────────────────────────────────────────

def sql_create_user(username: str, password: str) -> str:
    """Build a CREATE USER statement using the password from the desired state."""
    return f'CREATE USER "{username}" IDENTIFIED BY "{password}"'


def sql_drop_user(username: str) -> str:
    """Build a DROP USER … CASCADE statement."""
    return f'DROP USER "{username}" CASCADE'


def sql_grant_role(username: str, role: str) -> str:
    """Build a GRANT <role> TO <user> statement."""
    return f'GRANT {role} TO "{username}"'


def sql_revoke_role(username: str, role: str) -> str:
    """Build a REVOKE <role> FROM <user> statement."""
    return f'REVOKE {role} FROM "{username}"'


# ─────────────────────────────────────────────────────────────────────────────
#  RECONCILIATION ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def run_sql_action(
    label: str,
    sql_statement: str,
    endpoint: str,
    admin_user: str,
    admin_password: str,
    timeout: int,
    request_delay: float,
    logger: logging.Logger,
) -> bool:
    """
    Execute a single DDL statement, log the outcome, and enforce the
    inter-request delay to avoid overwhelming ORDS with concurrent calls.
    """
    result = execute_sql(endpoint, sql_statement, admin_user, admin_password, timeout, logger)
    success = result is not None

    if success:
        logger.info(f"  [OK]  {label}")
    else:
        logger.error(f"  [KO]  {label}")

    time.sleep(request_delay)
    return success


def apply_changes_for_database(
    db_name: str,
    verified_changes: dict,
    desired_state: dict,
    endpoint: str,
    admin_user: str,
    admin_password: str,
    timeout: int,
    request_delay: float,
    logger: logging.Logger,
) -> None:
    """
    Execute all verified changes for one database in dependency order:
    1. Create users first (they must exist before role grants)
    2. Grant roles
    3. Revoke roles
    4. Drop users last (CASCADE removes grants automatically)
    """
    logger.info(f"┌─────────────────────────────────────┐")
    logger.info(f"│  Applying changes: {db_name:<17}│")
    logger.info(f"└─────────────────────────────────────┘")

    for username in verified_changes.get("create_user", []):
        password = desired_state.get(username, {}).get("password", "")
        sql = sql_create_user(username, password)
        run_sql_action(
            f"CREATE USER {username}", sql,
            endpoint, admin_user, admin_password, timeout, request_delay, logger
        )

    for username, roles in verified_changes.get("grant_role", {}).items():
        for role in sorted(roles):
            sql = sql_grant_role(username, role)
            run_sql_action(
                f"GRANT {role} TO {username}", sql,
                endpoint, admin_user, admin_password, timeout, request_delay, logger
            )

    for username, roles in verified_changes.get("revoke_role", {}).items():
        for role in sorted(roles):
            sql = sql_revoke_role(username, role)
            run_sql_action(
                f"REVOKE {role} FROM {username}", sql,
                endpoint, admin_user, admin_password, timeout, request_delay, logger
            )

    for username in verified_changes.get("drop_user", []):
        sql = sql_drop_user(username)
        run_sql_action(
            f"DROP USER {username} CASCADE", sql,
            endpoint, admin_user, admin_password, timeout, request_delay, logger
        )

    logger.info("")


# ─────────────────────────────────────────────────────────────────────────────
#  ENVIRONMENT VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def resolve_db_credentials(db_name: str, db_config: dict) -> tuple[str, str]:
    """
    Resolve the admin username and password for a given database instance.

    The admin username is read from the JSON config (non-sensitive).
    The password is read from the environment variable <DB_NAME>_ORDS_ADMIN_PASSWORD
    so that secrets never appear in version-controlled files.
    """
    admin_user = db_config.get("ords_admin_user", "ADMIN")
    env_key    = f"{db_name}_ORDS_ADMIN_PASSWORD"
    password   = os.environ.get(env_key, "")
    return admin_user, password


def validate_environment(db_names: list, db_configs: dict, logger: logging.Logger) -> None:
    """
    Fail fast if any per-database password variable is missing, listing all
    absent keys at once so the operator can fix them in a single pass.
    """
    missing = [
        f"{db_name}_ORDS_ADMIN_PASSWORD"
        for db_name in db_names
        if not os.environ.get(f"{db_name}_ORDS_ADMIN_PASSWORD")
    ]
    if missing:
        logger.error(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Check your .env file."
        )
        sys.exit(EXIT_CONFIG_ERROR)


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    """
    Orchestrate the full multi-database reconciliation cycle:
    load config → for each database: fetch live state, compute diff →
    display consolidated plan → single confirmation → apply per database.
    """
    print("\n╔══════════════════════════════════════╗")
    print("║    Oracle ATP User Reconciler        ║")
    print("╚══════════════════════════════════════╝\n")

    logger = setup_logger(LOG_DIR)

    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ATP_USERS_FILE)
    if not os.path.isfile(config_path):
        logger.error(f"Configuration file not found: {config_path}")
        sys.exit(EXIT_CONFIG_ERROR)

    with open(config_path, encoding="utf-8") as config_file:
        config = json.load(config_file)

    databases = config.get("databases", {})
    if not databases:
        logger.error("No 'databases' key found in the configuration file.")
        sys.exit(EXIT_CONFIG_ERROR)

    validate_environment(list(databases.keys()), databases, logger)

    # ── Build verified planned changes for every database ─────────────────────
    all_verified_changes: dict = {}
    all_desired_states: dict   = {}
    all_endpoints: dict        = {}
    all_credentials: dict      = {}

    for db_name, db_config in databases.items():
        logger.info(f"Database: {db_name}")

        ords_base_url = db_config.get("ords_base_url", "")
        if not ords_base_url:
            logger.error(f"[{db_name}] Missing 'ords_base_url'. Skipping.")
            continue

        admin_user, admin_password = resolve_db_credentials(db_name, db_config)
        endpoint = build_sql_endpoint(ords_base_url, admin_user, ORDS_SQL_PATH)
        logger.debug(f"[{db_name}] ORDS SQL endpoint: {endpoint} (user: {admin_user})")

        desired_state = config_to_desired_state(db_config)
        current_state = fetch_current_users(
            endpoint, admin_user, admin_password, HTTP_TIMEOUT, db_name, logger
        )

        logger.debug(f"[{db_name}] Desired state: {json.dumps(desired_state, indent=2)}")
        logger.debug(f"[{db_name}] Current state: {json.dumps(current_state, indent=2)}")

        planned  = plan_changes(desired_state, current_state)
        verified = verify_planned_changes(planned, db_config)

        logger.debug(f"[{db_name}] Planned  changes: {planned}")
        logger.debug(f"[{db_name}] Verified changes: {verified}")

        all_verified_changes[db_name] = verified
        all_desired_states[db_name]   = desired_state
        all_endpoints[db_name]        = endpoint
        all_credentials[db_name]      = (admin_user, admin_password)

    # ── Display consolidated plan and ask for a single confirmation ───────────
    present_actions(all_verified_changes, logger)

    has_changes = any(
        any(bool(v) for v in changes.values())
        for changes in all_verified_changes.values()
    )

    if not has_changes:
        logger.info("Nothing to do. Exiting.")
        return

    if not request_operator_confirmation(VALIDATION_KEYWORD):
        logger.warning("[CANCELED] No changes were applied.")
        sys.exit(EXIT_CANCELED)

    logger.info("┌─────────────────────────────────────┐")
    logger.info("│           APPLYING CHANGES          │")
    logger.info("└─────────────────────────────────────┘\n")

    for db_name, verified_changes in all_verified_changes.items():
        admin_user, admin_password = all_credentials[db_name]
        apply_changes_for_database(
            db_name,
            verified_changes,
            all_desired_states[db_name],
            all_endpoints[db_name],
            admin_user,
            admin_password,
            HTTP_TIMEOUT,
            REQUEST_DELAY,
            logger,
        )

    logger.info("[OK] All done.")


if __name__ == "__main__":
    main()