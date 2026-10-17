"""Import leads from a CSV (export of the old Google Sheet) into Supabase.

Expected columns: LeadID, CompanyName, Website, ContactName, Email, Country,
Industry, Notes. Extra columns are ignored.

Usage:
    python scripts/import_leads.py leads.csv --dry-run
    python scripts/import_leads.py leads.csv
"""
import argparse
import sys
from pathlib import Path

from zypher_agent.config import Settings
from zypher_agent.db import PostgresRepo
from zypher_agent.leads import import_rows, parse_csv


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    parsed = parse_csv(args.csv_path)
    print(f"Valid rows: {len(parsed.rows)} | skipped: {len(parsed.skipped)}")
    for line, reason in parsed.skipped:
        print(f"  line {line}: {reason}")
    if args.dry_run:
        print("Dry run: nothing written.")
        return 0

    url = Settings().database_url
    if url is None:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 1
    imported, suppressed = import_rows(PostgresRepo(url.get_secret_value()), parsed.rows)
    print(f"Imported {imported} contacts, {suppressed} suppressed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
