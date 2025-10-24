#!/bin/bash
# Pull Docker images from Docker Hub and convert to Apptainer .sif format on HPC
#
# Usage:
#   ./pull_images_on_hpc.sh --dockerhub-user yourname --dockerhub-repo openhands-swebench
#
# Options:
#   --dockerhub-user USER      Docker Hub username (required)
#   --dockerhub-repo REPO      Docker Hub repository name (default: openhands-swebench)
#   --sif-dir DIR              Directory to store .sif files (default: .apptainer_cache/images)
#   --cache-dir DIR            Apptainer cache directory (default: .apptainer_cache)
#   --instance-ids IDS         Comma-separated instance IDs to pull (pulls all if not specified)
#   --parallel N               Number of parallel pulls (default: 1)

set -e

# Default values
DOCKERHUB_REPO="openhands-swebench"
SIF_DIR="$(pwd)/.apptainer_cache/images"
CACHE_DIR="$(pwd)/.apptainer_cache"
PARALLEL=1
INSTANCE_IDS=""
DOCKERHUB_USER=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --dockerhub-user)
            DOCKERHUB_USER="$2"
            shift 2
            ;;
        --dockerhub-repo)
            DOCKERHUB_REPO="$2"
            shift 2
            ;;
        --sif-dir)
            SIF_DIR="$2"
            shift 2
            ;;
        --cache-dir)
            CACHE_DIR="$2"
            shift 2
            ;;
        --instance-ids)
            INSTANCE_IDS="$2"
            shift 2
            ;;
        --parallel)
            PARALLEL="$2"
            shift 2
            ;;
        --help)
            grep "^#" "$0" | grep -v "^#!/" | sed 's/^# //'
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Check required arguments
if [ -z "$DOCKERHUB_USER" ]; then
    echo "Error: --dockerhub-user is required"
    echo "Use --help for usage information"
    exit 1
fi

# Create directories
mkdir -p "$SIF_DIR"
mkdir -p "$CACHE_DIR/tmp"

# Set Apptainer environment variables
export APPTAINER_CACHEDIR="$CACHE_DIR"
export APPTAINER_TMPDIR="$CACHE_DIR/tmp"

echo "=========================================="
echo "Apptainer Image Pull Configuration"
echo "=========================================="
echo "Docker Hub User: $DOCKERHUB_USER"
echo "Docker Hub Repo: $DOCKERHUB_REPO"
echo "SIF Directory: $SIF_DIR"
echo "Cache Directory: $CACHE_DIR"
echo "Parallel Jobs: $PARALLEL"
echo "=========================================="
echo ""

# Function to pull a single image
pull_image() {
    local instance_id="$1"
    local image_tag="sweb.eval.x86_64.${instance_id//__/_s_}"
    local docker_image="${DOCKERHUB_USER}/${DOCKERHUB_REPO}:${image_tag}"
    local sif_file="${SIF_DIR}/${image_tag}.sif"

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Processing: $instance_id"

    # Check if .sif file already exists
    if [ -f "$sif_file" ]; then
        echo "  ✓ Already exists: $sif_file"
        return 0
    fi

    echo "  Pulling: $docker_image"
    echo "  Target: $sif_file"

    # Pull and convert to .sif
    if apptainer pull "$sif_file" "docker://$docker_image" 2>&1 | tee /tmp/pull_${instance_id}.log; then
        echo "  ✓ Success: $sif_file"
        rm -f /tmp/pull_${instance_id}.log
        return 0
    else
        echo "  ✗ Failed: See /tmp/pull_${instance_id}.log"
        return 1
    fi
}

export -f pull_image
export DOCKERHUB_USER DOCKERHUB_REPO SIF_DIR

# Get instance IDs to process
if [ -n "$INSTANCE_IDS" ]; then
    # Use provided instance IDs
    IFS=',' read -ra INSTANCES <<< "$INSTANCE_IDS"
else
    # Try to read from a file (if it exists)
    if [ -f "instance_ids.txt" ]; then
        echo "Reading instance IDs from instance_ids.txt"
        mapfile -t INSTANCES < instance_ids.txt
    else
        echo "Error: No instance IDs specified."
        echo "Either use --instance-ids or create instance_ids.txt with one instance ID per line"
        exit 1
    fi
fi

echo "Total instances to pull: ${#INSTANCES[@]}"
echo ""

# Pull images
successful=0
failed=0

if [ "$PARALLEL" -gt 1 ]; then
    echo "Pulling images in parallel (max $PARALLEL jobs)..."

    # Use GNU parallel if available, otherwise use xargs
    if command -v parallel &> /dev/null; then
        printf '%s\n' "${INSTANCES[@]}" | parallel -j "$PARALLEL" pull_image {}
    else
        printf '%s\n' "${INSTANCES[@]}" | xargs -I {} -P "$PARALLEL" bash -c 'pull_image "$@"' _ {}
    fi
else
    echo "Pulling images sequentially..."
    for instance_id in "${INSTANCES[@]}"; do
        if pull_image "$instance_id"; then
            ((successful++))
        else
            ((failed++))
        fi
        echo ""
    done
fi

# Count results
successful=$(find "$SIF_DIR" -name "sweb.eval.x86_64.*.sif" -type f | wc -l)

echo ""
echo "=========================================="
echo "PULL SUMMARY"
echo "=========================================="
echo "Total instances: ${#INSTANCES[@]}"
echo "Successful: $successful"
echo "Failed: $failed"
echo "=========================================="
echo ""
echo "SIF files location: $SIF_DIR"
echo ""

# List all .sif files
echo "Available .sif files:"
ls -lh "$SIF_DIR"/*.sif 2>/dev/null || echo "  (none)"
echo ""

# Generate configuration for run_infer.sh
echo "=========================================="
echo "Configuration for run_infer.sh"
echo "=========================================="
cat << 'EOF'
# Add these lines to your run_infer.sh:

export RUNTIME=apptainer
export APPTAINER_CACHEDIR="$(pwd)/.apptainer_cache"
export APPTAINER_TMPDIR="$(pwd)/.apptainer_cache/tmp"

# Point to the directory containing .sif files
export EVAL_CONTAINER_IMAGE_PREFIX="$(pwd)/.apptainer_cache/images"

# Or set individual .sif file path in Python code
export PREBUILT_SIF_PATH="$(pwd)/.apptainer_cache/images/sweb.eval.x86_64.scikit-learn_s_scikit-learn-25500.sif"
EOF
echo "=========================================="

exit $failed
