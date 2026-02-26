import oci
import json
import logging
import sys
from datetime import datetime
from typing import Dict, List, Optional, Set
import os as _os





# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────
OCI_IDENTITY_USERS_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "oci_identity_users.json")
REQUIRE_VALIDATION      = True
VALIDATION_KEYWORD      = "OK"
LOG_FILE                = f"oci_reconcile_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

# ─────────────────────────────────────────────
#  LOGGING SETUP
# ─────────────────────────────────────────────
def setup_logger() -> logging.Logger:
    """Configure a logger that writes to both console and a log file."""
    logger = logging.getLogger("oci_reconcile")
    logger.setLevel(logging.DEBUG)

    # Console handler — clean, human-readable (no timestamp, no level for INFO)
    class ConsoleFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            if record.levelno == logging.INFO:
                return record.getMessage()
            elif record.levelno == logging.WARNING:
                return f"[WARNING] {record.getMessage()}"
            elif record.levelno == logging.ERROR:
                return f"[ERROR]   {record.getMessage()}"
            elif record.levelno == logging.DEBUG:
                return f"   {record.getMessage()}"
            return record.getMessage()

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(ConsoleFormatter())

    # File handler — full structured trace with timestamps and levels
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger

log = setup_logger()

# ─────────────────────────────────────────────
#  OCI HELPERS
# ─────────────────────────────────────────────
def get_identity_client(config: dict, domain_url: str) -> oci.identity_domains.IdentityDomainsClient:
    return oci.identity_domains.IdentityDomainsClient(config, service_endpoint=domain_url)


def export_users_from_domain(client: oci.identity_domains.IdentityDomainsClient, domain_name: str) -> dict:
    """
    Returns {username: [group1, group2, ...]} for all users in the domain.
    Handles pagination automatically.
    """
    log.info(f"[{domain_name}] Exporting current users from OCI domain …")
    users_dict = {}
    next_page = None

    while True:
        response = client.list_users(page=next_page, attributes="groups")
        for user in response.data.resources:
            groups = []
            if hasattr(user, "groups") and user.groups:
                groups = [g.display for g in user.groups]
            users_dict[user.user_name] = groups
            log.debug(f"[{domain_name}]   Found user: {user.user_name} | groups: {groups}")

        next_page = response.headers.get("opc-next-page")
        if not next_page:
            break

    log.info(f"  Fetching current state … ({len(users_dict)} user(s) found)\n")
    return users_dict


# ─────────────────────────────────────────────
#  CONFIG PARSING
# ─────────────────────────────────────────────
def config_to_users_dict(domain: dict) -> dict:
    """
    Extract {username: [groups]} for users in 'present' state.
    Groups can be declared directly OR via a profile name defined in domain['profiles'].
    """
    profiles = domain.get("profiles", {})
    result = {}
    for u in domain.get("users", []):
        if u.get("state") != "present":
            continue
        # Direct groups list
        groups = list(u.get("groups", []))
        # Optional profile — merge its groups
        profile_name = u.get("profile")
        if profile_name:
            profile_groups = profiles.get(profile_name, {}).get("groups", [])
            for g in profile_groups:
                if g not in groups:
                    groups.append(g)
        result[u["username"]] = groups
    return result


def get_user_config(domain: dict, username: str) -> Optional[dict]:
    """Return the full user config dict for a given username, or None."""
    for u in domain.get("users", []):
        if u["username"] == username:
            return u
    return None


# ─────────────────────────────────────────────
#  PLANNING
# ─────────────────────────────────────────────
def plan_changes(desired: dict, current: dict) -> dict:
    """Compute the diff between desired and current state."""
    changes = {
        "create_user": [],
        "remove_user": [],
        "add_user_to_group": {},
        "remove_user_from_group": {},
    }

    desired_set = set(desired.keys())
    current_set = set(current.keys())

    changes["create_user"] = list(desired_set - current_set)
    changes["remove_user"] = list(current_set - desired_set)

    # Group membership changes (only for users that exist in both states)
    for user in desired_set & current_set:
        to_add    = set(desired[user]) - set(current[user])
        to_remove = set(current[user]) - set(desired[user])
        if to_add:
            changes["add_user_to_group"][user] = to_add
        if to_remove:
            changes["remove_user_from_group"][user] = to_remove

    # Also compute group additions for users being created
    for user in changes["create_user"]:
        if desired.get(user):
            changes["add_user_to_group"][user] = set(desired[user])

    return changes


def verify_planned_changes(planned: dict, domain: dict) -> dict:
    """
    Filter out changes that are inconsistent with the declared state in the
    config file (e.g. don't delete a user not explicitly marked 'absent').
    """
    verified = {k: v.copy() if isinstance(v, list) else dict(v) for k, v in planned.items()}

    # Only create users explicitly marked 'present'
    verified_create = []
    for u in planned["create_user"]:
        cfg = get_user_config(domain, u)
        if cfg and cfg.get("state") == "present":
            verified_create.append(u)
    verified["create_user"] = verified_create

    # Only remove users explicitly marked 'absent' in the config
    # Users not mentioned in the config at all are LEFT UNTOUCHED
    verified_remove = []
    for u in planned["remove_user"]:
        cfg = get_user_config(domain, u)
        if cfg is not None and cfg.get("state") == "absent":
            verified_remove.append(u)
        else:
            log.debug(f"Skipping deletion of '{u}' — not explicitly marked 'absent' in config.")
    verified["remove_user"] = verified_remove

    return verified


# ─────────────────────────────────────────────
#  DISPLAY & VALIDATION
# ─────────────────────────────────────────────
def present_actions(all_changes: dict) -> None:
    log.info("┌─────────────────────────────────────┐")
    log.info("│           PLANNED ACTIONS           │")
    log.info("└─────────────────────────────────────┘")
    action_number = 0
    for domain_name, changes in all_changes.items():
        log.info(f"\n  Domain: {domain_name}")
        for user in changes["create_user"]:
            action_number += 1
            log.info(f"  {action_number:>2}. [+] CREATE  {user}")
        for user in changes["remove_user"]:
            action_number += 1
            log.info(f"  {action_number:>2}. [-] DELETE  {user}")
        for user, groups in changes["add_user_to_group"].items():
            action_number += 1
            log.info(f"  {action_number:>2}. [>] ADD     {user}  ->  {', '.join(groups)}")
        for user, groups in changes["remove_user_from_group"].items():
            action_number += 1
            log.info(f"  {action_number:>2}. [<] REMOVE  {user}  <-  {', '.join(groups)}")

    if action_number == 0:
        log.info("\n  [OK] Everything is already in the desired state. No changes needed.")
    log.info("")


def action_validation(keyword: str) -> bool:
    if not REQUIRE_VALIDATION:
        return True
    print("┌─────────────────────────────────────┐")
    print("│           VALIDATION                │")
    print("└─────────────────────────────────────┘")
    user_input = input(f"\n  Type '{keyword}' to confirm, anything else to cancel.\n  > ")
    approved = user_input.strip().lower() == keyword.lower()
    print()
    return approved


# ─────────────────────────────────────────────
#  GROUP RESOLUTION
# ─────────────────────────────────────────────
def get_group_id_by_name(client: oci.identity_domains.IdentityDomainsClient,
                         group_name: str, domain_name: str) -> Optional[str]:
    """Return the OCID/id of a group by display name, or None if not found."""
    try:
        response = client.list_groups(filter=f'displayName eq "{group_name}"', attributes="id,displayName")
        resources = response.data.resources or []
        if resources:
            return resources[0].id
        log.warning(f"[{domain_name}] Group '{group_name}' not found.")
        return None
    except Exception as e:
        log.error(f"[{domain_name}] Error looking up group '{group_name}': {e}")
        return None


def get_user_id_by_name(client: oci.identity_domains.IdentityDomainsClient,
                        username: str, domain_name: str) -> Optional[str]:
    """Return the OCI user id for a given username, or None if not found."""
    try:
        response = client.list_users(filter=f'userName eq "{username}"', attributes="id,userName")
        resources = response.data.resources or []
        if resources:
            return resources[0].id
        log.warning(f"[{domain_name}] User '{username}' not found in OCI.")
        return None
    except Exception as e:
        log.error(f"[{domain_name}] Error looking up user '{username}': {e}")
        return None


# ─────────────────────────────────────────────
#  RECONCILIATION ACTIONS
# ─────────────────────────────────────────────
def create_user(client: oci.identity_domains.IdentityDomainsClient,
                user_cfg: dict, domain_name: str) -> Optional[str]:
    """Create a user in OCI and return its new id, or None on failure."""
    username = user_cfg["username"]
    try:
        user_data = oci.identity_domains.models.User(
            schemas=["urn:ietf:params:scim:schemas:core:2.0:User"],
            user_name=username,
            name=oci.identity_domains.models.UserName(
                family_name=user_cfg.get("last_name", ""),
                given_name=user_cfg.get("first_name", ""),
            ),
            emails=[
                oci.identity_domains.models.UserEmails(
                    value=user_cfg["email"],
                    type="work",
                    primary=True,
                )
            ],
        )
        response = client.create_user(user=user_data)
        new_id = response.data.id
        log.info(f"  [OK]  [{domain_name}] User created: {username} (id={new_id})")
        return new_id
    except Exception as e:
        log.error(f"  [ERROR] [{domain_name}] Failed to create user '{username}': {e}")
        return None


def delete_user(client: oci.identity_domains.IdentityDomainsClient,
                username: str, domain_name: str) -> bool:
    user_id = get_user_id_by_name(client, username, domain_name)
    if not user_id:
        return False
    try:
        client.delete_user(user_id=user_id, force_delete=True)
        log.info(f"  [OK]  [{domain_name}] User deleted: {username}")
        return True
    except Exception as e:
        log.error(f"  [ERROR] [{domain_name}] Failed to delete user '{username}': {e}")
        return False


def add_user_to_group(client: oci.identity_domains.IdentityDomainsClient,
                      username: str, group_name: str,
                      domain_name: str, user_id: Optional[str] = None) -> bool:
    if not user_id:
        user_id = get_user_id_by_name(client, username, domain_name)
    group_id = get_group_id_by_name(client, group_name, domain_name)
    if not user_id or not group_id:
        return False
    try:
        patch_op = oci.identity_domains.models.PatchOp(
            schemas=["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            operations=[
                oci.identity_domains.models.Operations(
                    op="ADD",
                    path="members",
                    value=[{"value": user_id, "type": "User"}],
                )
            ],
        )
        client.patch_group(group_id=group_id, patch_op=patch_op)
        log.info(f"  [OK]  [{domain_name}] User '{username}' added to group '{group_name}'")
        return True
    except Exception as e:
        log.error(f"  [ERROR] [{domain_name}] Failed to add '{username}' to group '{group_name}': {e}")
        return False


def remove_user_from_group(client: oci.identity_domains.IdentityDomainsClient,
                           username: str, group_name: str,
                           domain_name: str) -> bool:
    user_id  = get_user_id_by_name(client, username, domain_name)
    group_id = get_group_id_by_name(client, group_name, domain_name)
    if not user_id or not group_id:
        return False
    try:
        patch_op = oci.identity_domains.models.PatchOp(
            schemas=["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            operations=[
                oci.identity_domains.models.Operations(
                    op="REMOVE",
                    path=f'members[value eq "{user_id}"]',
                )
            ],
        )
        client.patch_group(group_id=group_id, patch_op=patch_op)
        log.info(f"  [OK]  [{domain_name}] User '{username}' removed from group '{group_name}'")
        return True
    except Exception as e:
        log.error(f"  [ERROR] [{domain_name}] Failed to remove '{username}' from group '{group_name}': {e}")
        return False


# ─────────────────────────────────────────────
#  MAIN RECONCILIATION LOOP
# ─────────────────────────────────────────────
def reconcile_domain(all_verified_changes: dict,
                     oci_identity_users: dict,
                     oci_config: dict) -> None:
    """Apply all verified planned changes across every domain."""

    # domains is a dict: { "Default": { domain_name, domain_url, users, ... }, ... }
    domain_lookup = oci_identity_users["domains"]

    for domain_name, changes in all_verified_changes.items():
        log.info(f"┌─────────────────────────────────────┐")
        log.info(f"│  Applying changes: {domain_name:<17}│")
        log.info(f"└─────────────────────────────────────┘")
        domain_cfg = domain_lookup.get(domain_name)
        if not domain_cfg:
            log.error(f"Domain '{domain_name}' not found in config. Skipping.")
            continue

        client = get_identity_client(oci_config, domain_cfg["domain_url"])

        # 1. Create users
        new_user_ids: Dict[str, str] = {}   # username → new OCI id (to reuse below)
        for username in changes.get("create_user", []):
            user_cfg = get_user_config(domain_cfg, username)
            if not user_cfg:
                log.warning(f"[{domain_name}] No config found for user '{username}'. Skipping creation.")
                continue
            new_id = create_user(client, user_cfg, domain_name)
            if new_id:
                new_user_ids[username] = new_id

        # 2. Add users to groups (includes newly created users)
        for username, groups in changes.get("add_user_to_group", {}).items():
            uid = new_user_ids.get(username)   # reuse id if just created
            for group_name in groups:
                add_user_to_group(client, username, group_name, domain_name, user_id=uid)

        # 3. Remove users from groups
        for username, groups in changes.get("remove_user_from_group", {}).items():
            for group_name in groups:
                remove_user_from_group(client, username, group_name, domain_name)

        # 4. Delete users  (done last to avoid dependency issues)
        for username in changes.get("remove_user", []):
            delete_user(client, username, domain_name)

        log.info("")  # spacing after domain block


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────
def main() -> None:
    print("\n╔══════════════════════════════════════╗")
    print("║     OCI Identity Reconciler          ║")
    print("╚══════════════════════════════════════╝\n")
    log.info(f"Log file: {LOG_FILE}\n")

    oci_config = oci.config.from_file()

    if not _os.path.isfile(OCI_IDENTITY_USERS_FILE):
        log.error(f"Config file not found: {OCI_IDENTITY_USERS_FILE}")
        sys.exit(1)

    with open(OCI_IDENTITY_USERS_FILE, encoding="utf-8") as f:
        oci_identity_users = json.load(f)

    # ── Build verified planned changes for every domain ──────────────────────
    all_verified_changes: dict = {}

    for domain_name, domain in oci_identity_users["domains"].items():
        log.info(f"Domain: {domain_name}")

        client        = get_identity_client(oci_config, domain["domain_url"])
        desired_state = config_to_users_dict(domain)
        current_state = export_users_from_domain(client, domain_name)

        log.debug(f"[{domain_name}] Desired state: {desired_state}")
        log.debug(f"[{domain_name}] Current state: {current_state}")

        planned  = plan_changes(desired_state, current_state)
        verified = verify_planned_changes(planned, domain)

        log.debug(f"[{domain_name}] Planned  changes: {planned}")
        log.debug(f"[{domain_name}] Verified changes: {verified}")

        all_verified_changes[domain_name] = verified

    # ── Display summary and ask for confirmation ──────────────────────────────
    present_actions(all_verified_changes)

    has_changes = any(
        any(v for v in changes.values())
        for changes in all_verified_changes.values()
    )

    if not has_changes:
        log.info("Nothing to do. Exiting.")
        return

    if action_validation(VALIDATION_KEYWORD):
        log.info("┌─────────────────────────────────────┐")
        log.info("│           APPLYING CHANGES          │")
        log.info("└─────────────────────────────────────┘\n")
        reconcile_domain(all_verified_changes, oci_identity_users, oci_config)
        log.info("[OK] All done.")
    else:
        log.warning("[CANCELED] Canceled. No changes were applied.")


if __name__ == "__main__":
    main()
