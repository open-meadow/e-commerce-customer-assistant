"""The reviewer-facing sheets are generated from the catalogue and cannot drift from it."""

import csv
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.tools import flow_texts  # noqa: E402

spec = importlib.util.spec_from_file_location("localization_review", ROOT / "scripts" / "localization_review.py")
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


def test_every_catalogue_sentence_is_described_for_the_reviewers():
    assert set(review.WHEN) == set(flow_texts.TEXTS["en"])  # a new sentence must say when it is shown
    assert set(review.HIGH) <= set(review.WHEN) and set(review.MEDIUM) <= set(review.WHEN)


def test_the_sheets_hold_every_sentence_in_three_languages(tmp_path):
    md, csv_path = review.render(tmp_path)
    rows = list(csv.reader(open(csv_path, encoding="utf-8-sig", newline="")))
    assert len(rows) - 1 == len(flow_texts.TEXTS["en"])
    header = rows[0]
    assert header[3:6] == ["english", "hinglish_draft", "hindi_draft"]
    assert "reviewer_verdict (ok / change / unclear)" in header
    for row in rows[1:]:
        assert row[3] == flow_texts.TEXTS["en"][row[1]] and row[4] == flow_texts.TEXTS["hinglish"][row[1]]
        assert row[5] == flow_texts.TEXTS["hi"][row[1]] and row[7:] == ["", "", "", ""]  # the reviewer's columns start blank
    text = md.read_text(encoding="utf-8")
    assert text.count("\n| ") >= len(flow_texts.TEXTS["en"]) and "DRAFT" in text


def test_the_committed_sheets_are_up_to_date(tmp_path):
    md, csv_path = review.render(tmp_path)
    docs = ROOT / "docs"
    assert (docs / "localization_review_hinglish_hindi.md").read_text(encoding="utf-8") == md.read_text(encoding="utf-8")
    # Compare the content, not the platform's line endings: Git may store the committed CSV with LF and a checkout may
    # convert it to CRLF, while the generator writes CRLF row endings. Every other byte must be identical.
    committed = (docs / "localization_review_hinglish_hindi.csv").read_bytes().replace(b"\r\n", b"\n")
    assert committed == csv_path.read_bytes().replace(b"\r\n", b"\n")
