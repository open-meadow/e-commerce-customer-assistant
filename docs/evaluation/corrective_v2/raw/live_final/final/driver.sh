#!/bin/bash
# Three runs of the original set and of the new sets, one commit. A run with any provider error is not scored and is repeated (at most twice).
cd /d/ecommerce-integrate
OLD=/USER_HOME/AppData/Local/Temp/claude/18fd1d3b580e9829/4603691b-e018-4058-8166-7a89c045a8a8/scratchpad
OUT=/USER_HOME/AppData/Local/Temp/claude/18fd1d3b580e9829/b564e879-0054-4587-9783-93b267f3ced1/scratchpad/live_final/final
for kind in orig new; do
  for i in 1 2 3; do
    for attempt in 1 2 3; do
      f=$OUT/${kind}_run${i}_attempt${attempt}.json
      if [ $kind = orig ]; then
        uv run python $OLD/live_eval.py --llm env --allow-live --retrieval production --cases app/db/data/eval/queries_conversational.csv --conversations app/db/data/eval/conversations.json --output $f > $f.log 2>&1
      else
        uv run python $OLD/live_eval.py --new-sets --llm env --allow-live --retrieval production --output $f > $f.log 2>&1
      fi
      valid=$(python -c "import json,sys; d=json.load(open(sys.argv[1].replace('/c/','C:/'),encoding='utf-8')); print(d['integrity']['run_valid_for_scoring'], d['integrity']['provider_error_replies']['total'])" $f 2>/dev/null)
      echo "$kind run $i attempt $attempt: $valid" >> $OUT/progress.txt
      case "$valid" in True*) cp $f $OUT/${kind}_run${i}.json; break;; esac
    done
  done
done
echo DONE >> $OUT/progress.txt
