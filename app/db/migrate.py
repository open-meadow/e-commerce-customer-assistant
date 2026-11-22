"""Operator entry point for schema migrations (Phase 2): ``python -m app.db.migrate``.

    python -m app.db.migrate --db-path <file> --backup-path <new file> --authorization-reference <ref> --scratch [--dry-run]

Only a scratch-database migration is authorised (owner decision OD-06), so ``--scratch`` is mandatory: the operator asserts
that ``--db-path`` is a scratch copy. The tool refuses ``app/db/data/app.db`` and ``app/db/data/app_d2.db``, a missing or existing
backup path, a database that is already current, and a database in use elsewhere. It prints a JSON report and exits 0 on
success, 2 when it refuses or fails (the database is left as it was). Nothing migrates automatically anywhere else.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from app.db.errors import MigrationError
from app.db.migrations import migrate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.db.migrate", description=__doc__.splitlines()[0])
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--backup-path", required=True)
    parser.add_argument("--authorization-reference", required=True)
    parser.add_argument("--scratch", action="store_true",
                        help="assert that --db-path is a scratch copy (the only authorised mode)")
    parser.add_argument("--dry-run", action="store_true", help="pre-flight only; writes nothing")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = migrate(args.db_path, args.backup_path, args.authorization_reference,
                         scratch=args.scratch, dry_run=args.dry_run)
    except MigrationError as exc:
        print(json.dumps({"result": "refused_or_failed", "reason": str(exc)}))
        return 2
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
