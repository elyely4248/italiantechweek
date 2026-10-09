"""Backfill contact-company associations from existing CRM graph evidence.

The inference is intentionally conservative: it only creates contact-company
links when the database already contains an explicit path through a deal,
ticket, or activity. It never matches by person/company names.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx

import db


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Infer missing contact-company associations from existing CRM associations."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create the inferred links. Without this flag, only prints a dry-run summary.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to an env file containing DATABASE_URL. Defaults to .env.",
    )
    parser.add_argument(
        "--base-url",
        help="Optional CRM API base URL. When provided, calls the API endpoint instead of connecting to PostgreSQL.",
    )
    args = parser.parse_args()

    _load_env(Path(args.env_file))
    if args.base_url:
        token = os.environ.get("CRM_TOKEN")
        if not token:
            raise SystemExit("CRM_TOKEN is required when using --base-url")
        async with httpx.AsyncClient(
            base_url=args.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=60.0,
        ) as client:
            response = await client.post(
                "/__infer-contact-company-associations",
                json={"dry_run": not args.apply},
            )
            response.raise_for_status()
            result = response.json()
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("DATABASE_URL is required")

    result = await db.infer_contact_company_associations(dry_run=not args.apply)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_main())
