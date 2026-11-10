"""Validate demo database initialization arguments.

Database creation and seeding are added in the next implementation step.
"""

from __future__ import annotations

import argparse
import re
from datetime import UTC, datetime
from pathlib import Path

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
    candidate = Path(value).expanduser().resolve()
    protected_database = (root / "data" / "app.db").resolve()
    if candidate == protected_database:
        raise argparse.ArgumentTypeError(
            "refusing to use the real project database at data/app.db"
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
        help="database file path (refuses the real project data/app.db)",
    )
    parser.add_argument(
        "--reference-time",
        required=True,
        type=parse_reference_time,
        help="timezone-aware ISO-8601 timestamp; normalized to UTC",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        db_path = resolve_safe_db_path(arguments.db_path)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))

    print(
        "Arguments validated. Database initialization will be added in B3-2 "
        f"(path: {db_path}, reference time: {arguments.reference_time.isoformat()})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
