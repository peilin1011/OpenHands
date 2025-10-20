#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || "${#}" -lt 2 ]]; then
  cat <<'USAGE'
Usage: build_openhands_swe_image.sh <instance-image> <target-tag> [runtime-image]

Example:
  ./build_openhands_swe_image.sh \
    swebench/sweb.eval.x86_64.scikit-learn_1776_scikit-learn-13439:latest \
    myrepo/openhands-swebench:scikit-learn \
    ghcr.io/all-hands-ai/runtime:latest

This builds a Docker image that layers the OpenHands runtime on top of the
specified SWE-Bench instance image. The resulting image can later be pushed to a
registry or converted into an Apptainer/Singularity SIF file.
USAGE
  exit 1
fi

INSTANCE_IMAGE="$1"
TARGET_TAG="$2"
RUNTIME_IMAGE="${3:-ghcr.io/all-hands-ai/runtime:latest}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

docker build \
  --build-arg INSTANCE_IMAGE="${INSTANCE_IMAGE}" \
  --build-arg RUNTIME_IMAGE="${RUNTIME_IMAGE}" \
  --file "${SCRIPT_DIR}/Dockerfile.openhands-swebench" \
  --tag "${TARGET_TAG}" \
  "${SCRIPT_DIR}"

echo "Built image ${TARGET_TAG}"
