#!/usr/bin/env bash
set -eo pipefail

source "evaluation/utils/version_control.sh"


export SWE_DATASET_LOCAL_PATH=/anvme/workspace/b273dd14-swe-openhands/OpenHands/datasets_cache/princeton-nlp__SWE-bench_Lite
# 如果希望完全离线，可再加
export HF_DATASETS_OFFLINE=1

export RUNTIME=apptainer
export APPTAINER_CACHEDIR=/anvme/workspace/b273dd14-swe-openhands/.apptainer_cache
export APPTAINER_TMPDIR=/anvme/workspace/b273dd14-swe-openhands/.apptainer_cache/tmp
export APPTAINER_RUNTIME_LOG_DIR=/anvme/workspace/b273dd14-swe-openhands/.apptainer_cache/logs

export HF_HOME="/anvme/workspace/b273dd14-swe-openhands/huggingface_cache"
export CUDA_VISIBLE_DEVICES=0,1,2,3
export NO_PROXY="localhost,127.0.0.1"
export no_proxy="localhost,127.0.0.1"

# Apptainer/Singularity 缓存目录设置（避免 home 目录配额问题）
mkdir -p "$APPTAINER_CACHEDIR" "$APPTAINER_TMPDIR"

model="/anvme/workspace/b273dd14-swe-openhands/huggingface_cache/Qwen3-32B"  # ✅ 使用本地模型路径
log_dir='logs'
TIMESTAMP=$(date +%Y%m%d-%H%M%S)

run_id="${1:-unspecified_run}"
vllm_log="$log_dir/vllm_${TIMESTAMP}_${run_id}.log"  # ✅ 简化日志名
swe_log_file="$log_dir/swe_${TIMESTAMP}_${run_id}.log"

mkdir -p $log_dir

#########################################################
# 端口配置
port=8003  # ✅ 只需要一个端口

echo "Port configuration:"
echo "  Unified model $model : $port"

# ✅ 只检查一个端口
if ss -lntu | awk 'NR>1 {print $5}' | sed 's/.*://' | grep -qw "$port"; then
    echo "Error: Port $port is already in use. Please free the port first." >&2
    exit 1
fi

#########################################################

# ✅ 简化的清理函数
cleanup() { 
    echo "Script interrupted or exiting. Cleaning up vLLM server..." >&2
    if [ -n "$vllm_pid" ] && ps -p "$vllm_pid" > /dev/null; then
        echo "Stopping vLLM server (PID: $vllm_pid)..." >&2
        kill "$vllm_pid"
        wait "$vllm_pid" 2>/dev/null 
    fi
    echo "vLLM server stopped." >&2
}
trap cleanup SIGINT SIGTERM EXIT

#########################################################
# 启动统一的 vLLM 服务器

echo ""
echo "🚀 Starting Unified vLLM Server: $model"

vllm serve $model \
    --tensor-parallel-size 4 \
    --reasoning-parser qwen3 \
    --enforce-eager \
    --gpu-memory-utilization 0.90 \
    --enable-auto-tool-choice \
    --tool-call-parser hermes \
    --rope-scaling '{"factor": 4.0, "original_max_position_embeddings": 32768, "rope_type": "yarn"}' \
    --enable-prefix-caching \
    --max-num-seqs 40 \
    --max-model-len $((128 * 1024 - 8 * 1024)) \
    --seed 41 \
    --port $port > $vllm_log 2>&1 &

vllm_pid=$!  # ✅ 使用统一的变量名

echo "vLLM server starting (PID: $vllm_pid, Port: $port)"

# 等待服务器初始化
timeout_minutes=9
start_time=$(date +%s)
timeout_seconds=$((timeout_minutes * 60))

echo "Waiting for vLLM to initialize (timeout: ${timeout_minutes} minutes)..."

while [ $(($(date +%s) - start_time)) -lt $timeout_seconds ]; do
    if ! ps -p $vllm_pid > /dev/null; then
        echo "❌ vLLM server process exited with an error"
        exit 1
    fi
    
    if [ -f "$vllm_log" ] && grep -q "Application startup complete." "$vllm_log"; then
        echo "✅ vLLM initialized successfully"
        break
    fi
    sleep 2
done

if [ $(($(date +%s) - start_time)) -ge $timeout_seconds ]; then
    echo "❌ vLLM initialization timed out"
    exit 1
fi

# ✅ 修复：正确的 cat <<EOF 格式
cat <<EOF

======================================================================
🎯 vLLM Server is Ready!
======================================================================
   Model: $model (Unified server for main + summary tasks)
   • PID: $vllm_pid
   • Port: $port
   • API Base: http://localhost:$port/v1
   • Log: $vllm_log
   • Max concurrent sequences: 40
======================================================================

💡 Starting mini-SWE-agent with workflow condenser...
🛑 Press Ctrl+C to stop the server

======================================================================
🚀 mini-SWE-agent: Qwen3-32B (Single Server)
======================================================================

EOF

MODEL_CONFIG=$1
COMMIT_HASH=$2
AGENT=$3
EVAL_LIMIT=$4
MAX_ITER=$5
NUM_WORKERS=$6
DATASET=$7
SPLIT=$8
N_RUNS=$9
MODE=${10}


if [ -z "$NUM_WORKERS" ]; then
  NUM_WORKERS=1
  echo "Number of workers not specified, use default $NUM_WORKERS"
fi
checkout_eval_branch

if [ -z "$AGENT" ]; then
  echo "Agent not specified, use default CodeActAgent"
  AGENT="CodeActAgent"
fi

if [ -z "$MAX_ITER" ]; then
  echo "MAX_ITER not specified, use default 100"
  MAX_ITER=100
fi

if [ -z "$RUN_WITH_BROWSING" ]; then
  echo "RUN_WITH_BROWSING not specified, use default false"
  RUN_WITH_BROWSING=false
fi


if [ -z "$DATASET" ]; then
  echo "DATASET not specified, use default princeton-nlp/SWE-bench_Lite"
  DATASET="princeton-nlp/SWE-bench_Lite"
fi

if [ -z "$SPLIT" ]; then
  echo "SPLIT not specified, use default test"
  SPLIT="test"
fi

if [ -z "$MODE" ]; then
  MODE="swe"
  echo "MODE not specified, use default $MODE"
fi

if [ -n "$EVAL_CONDENSER" ]; then
  echo "Using Condenser Config: $EVAL_CONDENSER"
else
  echo "No Condenser Config provided via EVAL_CONDENSER, use default (NoOpCondenser)."
fi

export RUN_WITH_BROWSING=$RUN_WITH_BROWSING
echo "RUN_WITH_BROWSING: $RUN_WITH_BROWSING"

get_openhands_version

echo "AGENT: $AGENT"
echo "OPENHANDS_VERSION: $OPENHANDS_VERSION"
echo "MODEL_CONFIG: $MODEL_CONFIG"
echo "DATASET: $DATASET"
echo "SPLIT: $SPLIT"
echo "MAX_ITER: $MAX_ITER"
echo "NUM_WORKERS: $NUM_WORKERS"
echo "COMMIT_HASH: $COMMIT_HASH"
echo "MODE: $MODE"
echo "EVAL_CONDENSER: $EVAL_CONDENSER"

# Default to NOT use Hint
if [ -z "$USE_HINT_TEXT" ]; then
  export USE_HINT_TEXT=false
fi
echo "USE_HINT_TEXT: $USE_HINT_TEXT"
EVAL_NOTE="$OPENHANDS_VERSION"
# if not using Hint, add -no-hint to the eval note
if [ "$USE_HINT_TEXT" = false ]; then
  EVAL_NOTE="$EVAL_NOTE-no-hint"
fi

if [ "$RUN_WITH_BROWSING" = true ]; then
  EVAL_NOTE="$EVAL_NOTE-with-browsing"
fi

if [ -n "$EXP_NAME" ]; then
  EVAL_NOTE="$EVAL_NOTE-$EXP_NAME"
fi
# if mode != swe, add mode to the eval note
if [ "$MODE" != "swe" ]; then
  EVAL_NOTE="${EVAL_NOTE}-${MODE}"
fi
# Add condenser config to eval note if provided
if [ -n "$EVAL_CONDENSER" ]; then
  EVAL_NOTE="${EVAL_NOTE}-${EVAL_CONDENSER}"
fi

function run_eval() {
  local eval_note="${1}"
  COMMAND="poetry run python evaluation/benchmarks/swe_bench/run_infer.py \
    --agent-cls $AGENT \
    --llm-config $MODEL_CONFIG \
    --max-iterations $MAX_ITER \
    --eval-num-workers $NUM_WORKERS \
    --eval-note $eval_note \
    --dataset $DATASET \
    --split $SPLIT \
    --mode $MODE"



  if [ -n "$EVAL_LIMIT" ]; then
    echo "EVAL_LIMIT: $EVAL_LIMIT"
    COMMAND="$COMMAND --eval-n-limit $EVAL_LIMIT"
  fi

  # Run the command
  eval $COMMAND
}

unset SANDBOX_ENV_GITHUB_TOKEN # prevent the agent from using the github token to push
if [ -z "$N_RUNS" ]; then
  N_RUNS=1
  echo "N_RUNS not specified, use default $N_RUNS"
fi

# Skip runs if the run number is in the SKIP_RUNS list
# read from env variable SKIP_RUNS as a comma separated list of run numbers
SKIP_RUNS=(${SKIP_RUNS//,/ })
for i in $(seq 1 $N_RUNS); do
  if [[ " ${SKIP_RUNS[@]} " =~ " $i " ]]; then
    echo "Skipping run $i"
    continue
  fi
  current_eval_note="$EVAL_NOTE-run_$i"
  echo "EVAL_NOTE: $current_eval_note"
  run_eval $current_eval_note
done

checkout_original_branch
if [ -n "$http_proxy" ]; then
  export APPTAINERENV_http_proxy="$http_proxy"
fi
if [ -n "$https_proxy" ]; then
  export APPTAINERENV_https_proxy="$https_proxy"
fi
