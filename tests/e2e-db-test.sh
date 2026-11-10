uv run python -m pytest -q tests/test_init_demo_db.py

tmpdir="$(mktemp -d)"
cd scripts

uv run python init_demo_db.py \
  --db-path "$tmpdir/demo.sqlite" \
  --reference-time 2026-11-07T00:00:00Z

if uv run python init_demo_db.py \
  --db-path "$tmpdir/demo.sqlite" \
  --reference-time 2026-11-07T00:00:00Z; then
  echo "ERROR: expected the populated database to be refused"
  exit 1
else
  echo "Populated database correctly refused"
fi

cd ..
git diff --check