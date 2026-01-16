#!/usr/bin/env bash

source ~/.bashrc
SWEUTIL_DIR=/swe_util

# FIXME: Cannot read SWE_INSTANCE_ID from the environment variable
# SWE_INSTANCE_ID=django__django-11099
if [ -z "$SWE_INSTANCE_ID" ]; then
    echo "Error: SWE_INSTANCE_ID is not set." >&2
    exit 1
fi

# Read the swe-bench-test-lite.json file and extract the required item based on instance_id
item=$(jq --arg INSTANCE_ID "$SWE_INSTANCE_ID" '.[] | select(.instance_id == $INSTANCE_ID)' $SWEUTIL_DIR/eval_data/instances/swe-bench-instance.json)

if [[ -z "$item" ]]; then
  echo "No item found for the provided instance ID."
  exit 1
fi


WORKSPACE_NAME=$(echo "$item" | jq -r '(.repo | tostring) + "__" + (.version | tostring) | gsub("/"; "__")')

echo "WORKSPACE_NAME: $WORKSPACE_NAME"

# Ensure /workspace exists (do not delete other instances' directories)
mkdir -p /workspace

# Initialize workspace for this specific instance
# Each instance has a unique directory name, so no locking is needed
WORKSPACE_DIR="/workspace/$WORKSPACE_NAME"

# Remove only this instance's workspace directory (not all contents of /workspace)
# This ensures each instance has a clean workspace without affecting others
if [ -d "$WORKSPACE_DIR" ]; then
    rm -rf "$WORKSPACE_DIR"
fi

# Copy repository from /testbed to workspace for this instance
# Each instance gets its own copy from /testbed
cp -r /testbed "$WORKSPACE_DIR"

# Verify the workspace was created successfully
if [ ! -d "$WORKSPACE_DIR" ]; then
    echo "Error: Failed to create workspace directory $WORKSPACE_DIR" >&2
    exit 1
fi

# Activate instance-specific environment
if [ -d /opt/miniconda3 ]; then
    . /opt/miniconda3/etc/profile.d/conda.sh
    conda activate testbed
fi
