#!/usr/bin/env bash
set -euo pipefail

# Edit the list below with the SWE-Bench instance images you need.
INSTANCES=(
  swebench/sweb.eval.x86_64.scikit-learn_1776_scikit-learn-13439:latest
  swebench/sweb.eval.x86_64.django_1776_django-11011:latest
)

REPO="${1:-local/openhands-swebench}"
RUNTIME_IMAGE="${2:-ghcr.io/all-hands-ai/runtime:latest}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for INSTANCE in "${INSTANCES[@]}"; do
  NAME="$(basename "${INSTANCE}")"
  TAG="${REPO}:${NAME}"
  "${SCRIPT_DIR}/build_openhands_swe_image.sh" "${INSTANCE}" "${TAG}" "${RUNTIME_IMAGE}"
done
