#!/usr/bin/env python3
"""Verify the published evaluation archive. Standard library only. Run from anywhere:

    python verify_archive.py                                  # integrity: every file against MANIFEST.md and REDACTIONS.md
    python verify_archive.py --check-account NAME             # also: no file contains NAME (any case, any path separator)
    python verify_archive.py --originals DIR --account NAME   # also: rebuild each redacted file from the ORIGINAL in DIR

The account name is an argument, never stored here or in REDACTIONS.md, because it is exactly what was redacted.
Exit code 0 when everything checks out, 1 when anything fails.
"""
import argparse
import hashlib
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NOT_EVIDENCE = {"MANIFEST.md", "REDACTIONS.md", ".gitattributes", "verify_archive.py"}
BS = "\\"
MANIFEST_ROW = re.compile(r"^\| `([^`]+)` \| (.*?) \| (.*?) \| (.*?) \| (.*?) \| (.*?) \| ([\d,]+) \| `([0-9a-f]{64})` \|$", re.M)
REDACTION_ROW = re.compile(r"^\| `([^`]+)` \| (\d) \| `([0-9a-f]{64})` \| `([0-9a-f]{64})` \| (\d+) \|$", re.M)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rules(account: str):
    """The three substitutions documented in REDACTIONS.md, as (original, published) byte strings, numbered from 1."""
    a = account.encode()
    return {
        1: (b"C:" + (BS * 2).encode() + b"Users" + (BS * 2).encode() + a + (BS * 2).encode(), b"<USER_HOME>" + (BS * 2).encode()),
        2: (b"C:" + BS.encode() + b"Users" + BS.encode() + a + BS.encode(), b"<USER_HOME>" + BS.encode()),
        3: (b"/c/Users/" + a + b"/", b"/USER_HOME/"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check-account", metavar="NAME", help="fail if any file in this folder contains NAME (case-insensitive)")
    ap.add_argument("--originals", metavar="DIR", help="folder holding the original, unredacted evidence files (same relative paths)")
    ap.add_argument("--account", metavar="NAME", help="the account name that was redacted (needed with --originals)")
    args = ap.parse_args()
    if bool(args.originals) != bool(args.account):
        ap.error("--originals and --account go together")

    failures = []
    manifest = MANIFEST_ROW.findall((HERE / "MANIFEST.md").read_text(encoding="utf-8"))
    redactions = REDACTION_ROW.findall((HERE / "REDACTIONS.md").read_text(encoding="utf-8"))
    print(f"manifest rows: {len(manifest)} | redaction rows: {len(redactions)}")

    # 1. every manifest row: file present, size and SHA-256 match; no evidence file missing from the manifest
    listed = set()
    for rel, _cat, _role, _run, _status, _note, size, digest in manifest:
        listed.add(rel)
        path = HERE / rel
        if not path.is_file():
            failures.append(f"missing: {rel}")
            continue
        data = path.read_bytes()
        if sha256(data) != digest or len(data) != int(size.replace(",", "")):
            failures.append(f"hash or size differs from MANIFEST.md: {rel}")
    on_disk = {p.relative_to(HERE).as_posix() for p in HERE.rglob("*") if p.is_file()} - NOT_EVIDENCE
    for extra in sorted(on_disk - listed):
        failures.append(f"file not in MANIFEST.md: {extra}")
    print(f"files recomputed against the manifest: {len(manifest) - len([f for f in failures if f.startswith(('missing', 'hash'))])} of {len(manifest)} ok")

    # 2. the redaction audit agrees with the manifest (published hash) and counts add up
    by_name = {r[0]: r for r in manifest}
    for rel, rule, original, published, count in redactions:
        if rel not in by_name or by_name[rel][7] != published:
            failures.append(f"REDACTIONS.md published hash differs from MANIFEST.md: {rel}")
    print(f"redacted files: {len(redactions)} | replacements recorded: {sum(int(r[4]) for r in redactions)}")

    # 3. optional: no file contains the account name
    if args.check_account:
        needle = re.compile(re.escape(args.check_account.encode()), re.I)
        hits = [p.relative_to(HERE).as_posix() for p in HERE.rglob("*") if p.is_file() and p.name != "verify_archive.py" and needle.search(p.read_bytes())]
        print(f"files containing the account name: {len(hits)}")
        failures += [f"contains the account name: {h}" for h in hits]

    # 4. optional: rebuild every redacted file from its original and compare with the published file
    if args.originals:
        table = rules(args.account)
        ok = 0
        for rel, rule, original, published, count in redactions:
            src = Path(args.originals) / rel
            if not src.is_file():
                failures.append(f"original not found: {rel}")
                continue
            data = src.read_bytes()
            if sha256(data) != original:
                failures.append(f"original SHA-256 differs from REDACTIONS.md: {rel}")
                continue
            old, new = table[int(rule)]
            if data.count(old) != int(count):
                failures.append(f"replacement count differs: {rel}")
                continue
            if sha256(data.replace(old, new)) != published:
                failures.append(f"rebuilt file differs from the published file: {rel}")
                continue
            ok += 1
        print(f"redacted files rebuilt from the originals and matching: {ok} of {len(redactions)}")

    for f in failures:
        print("FAIL:", f)
    print("RESULT:", "all checks passed" if not failures else f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
