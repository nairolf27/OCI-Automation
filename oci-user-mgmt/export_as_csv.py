"""
Filename: export_domains.py
Description: Exports users from a JSON domain file into one CSV per domain,
             with columns: first_name, last_name, profile, groups.
Author: Florian NOEL
Date: 11/03/2026
"""

import csv
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path("logs") / "export_domains.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------

load_dotenv()

INPUT_FILE = os.getenv("INPUT_FILE", "domains.json")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "exports")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CSV_FIELDNAMES = ["first_name", "last_name", "profile", "groups"]
CSV_DELIMITER = ","


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def load_json(file_path: str) -> dict:
    """
    Load and parse a JSON file from disk.

    :param file_path: Path to the JSON file to read.
    :return: Parsed content as a dictionary.
    :raises SystemExit: If the file is not found or contains invalid JSON.
    """
    path = Path(file_path)
    if not path.exists():
        logger.error("Input file not found: %s", file_path)
        sys.exit(1)

    with path.open(encoding="utf-8") as f:
        try:
            data = json.load(f)
            logger.info("Loaded JSON file: %s", file_path)
            return data
        except json.JSONDecodeError as exc:
            logger.error("Invalid JSON in %s: %s", file_path, exc)
            sys.exit(1)


def export_domain_to_csv(
    domain_name: str,
    users: list[dict],
    output_dir: Path,
) -> None:
    """
    Write all users of a single domain to a CSV file.

    Each row contains first_name, last_name, profile (raw, may be empty),
    and groups (semicolon-separated). The output file is named after the domain.

    :param domain_name: Name of the domain, used as the output filename stem.
    :param users: List of user dictionaries for this domain.
    :param output_dir: Directory where the CSV file will be written.
    """
    output_path = output_dir / f"{domain_name}.csv"

    with output_path.open(mode="w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDNAMES, delimiter=CSV_DELIMITER)
        writer.writeheader()

        for user in users:
            groups = user.get("groups", [])
            writer.writerow({
                "first_name": user.get("first_name", ""),
                "last_name": user.get("last_name", ""),
                "profile": user.get("profile", ""),
                "groups": ";".join(groups),
            })

    logger.info("Exported domain '%s' → %s (%d users)", domain_name, output_path, len(users))


def export_all_domains(data: dict, output_dir: Path) -> None:
    """
    Iterate over all domains in the JSON data and export each to its own CSV.

    :param data: Full parsed JSON structure containing a 'domains' key.
    :param output_dir: Directory where CSV files will be created.
    """
    domains = data.get("domains", {})
    if not domains:
        logger.warning("No domains found in the input file.")
        return

    output_dir.mkdir(parents=True, exist_ok=True)

    for domain_name, domain_data in domains.items():
        users = domain_data.get("users", [])
        logger.debug("Processing domain '%s' with %d users", domain_name, len(users))
        export_domain_to_csv(domain_name, users, output_dir)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """
    Entry point: loads the JSON input file and exports each domain to a CSV.
    """
    Path("logs").mkdir(exist_ok=True)
    logger.info("Starting domain export — input: %s, output dir: %s", INPUT_FILE, OUTPUT_DIR)

    data = load_json(INPUT_FILE)
    export_all_domains(data, Path(OUTPUT_DIR))

    logger.info("Export complete.")


if __name__ == "__main__":
    main()
