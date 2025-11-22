#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: build_and_push_swebench.sh <dataset> <repo> [runtime-image] [split] [push?]

Arguments:
  dataset        HuggingFace dataset id (e.g. princeton-nlp/SWE-bench_Lite)
  repo           Docker repository prefix for output images (e.g. username/openhands-swebench)
  runtime-image  OpenHands runtime base image (default: ghcr.io/all-hands-ai/runtime:latest)
  split          Dataset split (default: test)
  push?          If "push" or "true", docker push will be executed for each built tag

Example:
  ./build_and_push_swebench.sh \
    princeton-nlp/SWE-bench_Lite \
    myuser/openhands-swebench \
    ghcr.io/all-hands-ai/runtime:latest \
    test \
    push

Requirements:
  - Docker CLI with buildx enabled
  - Python + `datasets` package (pip install datasets)
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || "${#}" -lt 2 ]]; then
  usage
  exit 1
fi

DATASET="$1"
REPO="$2"
RUNTIME_IMAGE="${3:-ghcr.io/all-hands-ai/runtime:latest}"
SPLIT="${4:-test}"
PUSH_FLAG="${5:-false}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

readarray -t INSTANCES < <(python3 - "$DATASET" "$SPLIT" <<'PY'
import sys
from datasets import load_dataset

dataset_id = sys.argv[1]
split = sys.argv[2]

SUPPORTED = {
    'princeton-nlp/SWE-bench_Multimodal',
    'princeton-nlp/SWE-bench',
    'princeton-nlp/SWE-bench_Lite',
    'princeton-nlp/SWE-bench_Verified',
}
if dataset_id not in SUPPORTED:
    raise SystemExit(f"Dataset {dataset_id} not supported")

def swebench_instance_to_image(instance_id: str) -> str:
    repo, name = instance_id.split('__')
    return f'swebench/sweb.eval.x86_64.{repo}_1776_{name}:latest'

def swebench_mm_instance_to_image(instance_id: str) -> str:
    repo, name = instance_id.split('__')
    return f'swebench/sweb.mm.eval.x86_64.{repo}_1776_{name}:latest'

data = load_dataset(dataset_id, split=split)
print(f"Loaded {len(data)} instances from {dataset_id}::{split}", file=sys.stderr)
for instance_id in data['instance_id']:
    if dataset_id in [
        'princeton-nlp/SWE-bench',
        'princeton-nlp/SWE-bench_Lite',
        'princeton-nlp/SWE-bench_Verified',
    ]:
        print(swebench_instance_to_image(instance_id))
    else:
        print(swebench_mm_instance_to_image(instance_id))
PY
)

if [[ "${#INSTANCES[@]}" -eq 0 ]]; then
  echo "No instances found for dataset ${DATASET}" >&2
  exit 1
fi

for INSTANCE in "${INSTANCES[@]}"; do
  NAME="$(basename "${INSTANCE}")"
  TAG="${REPO}:${NAME}"
  "${SCRIPT_DIR}/build_openhands_swe_image.sh" "${INSTANCE}" "${TAG}" "${RUNTIME_IMAGE}"

  if [[ "${PUSH_FLAG,,}" == "push" || "${PUSH_FLAG,,}" == "true" ]]; then
    docker push "${TAG}"
  fi
done
