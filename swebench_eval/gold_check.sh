#!/usr/bin/env bash
# Build Docker images locally (native arm64) and validate candidates with their gold patches.
# Usage: bash gold_check.sh            -> all candidates
#        bash gold_check.sh <id> ...   -> only the given instance ids (quick smoke test)
set -euo pipefail
cd "$(dirname "$0")"
if [ $# -gt 0 ]; then IDS="$*"; RUN_ID=gold_smoke; else IDS=$(cat data/candidate_ids.txt); RUN_ID=gold_check; fi
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --split test \
  --predictions_path gold \
  --instance_ids $IDS \
  --max_workers ${WORKERS:-2} \
  --namespace '' \
  --cache_level instance \
  --run_id $RUN_ID 2>&1 | tee data/$RUN_ID.log
