"""Initialize a demo database at an explicit path and reference time."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.db.connection import get_initialization_connection
from app.db.schema import create_schema
from app.db.seed_data import seed_database

_ISO8601_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def parse_reference_time(value: str) -> datetime:
    """Parse a timezone-aware ISO-8601 timestamp and normalize it to UTC."""
    if not _ISO8601_TIMESTAMP.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "reference time must be an ISO-8601 timestamp with timezone, "
            "for example 2026-11-07T00:00:00Z"
        )

    normalized_value = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized_value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid ISO-8601 reference time: {value}"
        ) from exc

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError(
            "reference time must include a timezone"
        )
    return parsed.astimezone(UTC)


def resolve_safe_db_path(value: str, *, project_root: Path | None = None) -> Path:
    """Resolve a database path and reject the real project database."""
    if not value.strip():
        raise argparse.ArgumentTypeError("database path must not be empty")

    root = (
        project_root.resolve()
        if project_root is not None
        else Path(__file__).resolve().parents[1]
    )
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    protected_database = (root / "app" / "db" / "data" / "app.db").resolve()
    if candidate == protected_database:
        raise argparse.ArgumentTypeError(
            "refusing to use the real project database at app/db/data/app.db"
        )
    return candidate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Initialize a demo database at an explicit path and reference time.",
        epilog=(
            "Demo timing: the 48-hour damaged scenario (ORD1005) is valid for "
            "12 hours after the reference time. The 7-day scenarios are valid "
            "for about 4 to 5.5 days."
        ),
    )
    parser.add_argument(
        "--db-path",
        required=True,
        help="database file path (refuses the real project app/db/data/app.db)",
    )
    parser.add_argument(
        "--reference-time",
        required=True,
        type=parse_reference_time,
        help="timezone-aware ISO-8601 timestamp; normalized to UTC",
    )
    return parser


def initialize_database(db_path: Path, reference_time: datetime) -> None:
    """Create the schema and seed an empty database, refusing existing objects."""
    connection = get_initialization_connection(str(db_path))
    try:
        existing_objects = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
            "LIMIT 1"
        ).fetchone()
        if existing_objects is not None:
            raise ValueError(
                f"database is not empty (found {existing_objects['name']!r}); "
                "refusing to initialize it"
            )

        create_schema(connection)
        seed_database(connection, reference_time=reference_time)
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        db_path = resolve_safe_db_path(arguments.db_path)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))

    try:
        initialize_database(db_path, arguments.reference_time)
    except ValueError as exc:
        parser.error(str(exc))

    print(f"Demo database initialized and seeded at {db_path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
