#!/bin/bash -l
#SBATCH --gres=gpu:a40:4   # 请求 GPU
#SBATCH --time=10:00:00     # 运行时间限制6小时
#SBATCH --job-name=0-50-without-conndenser-2
#SBATCH --export=NONE       # 不继承提交环境

# ============================================================================
# SLURM 任务配置与环境初始化
# ============================================================================
unset SLURM_EXPORT_ENV      # 允许环境传递给srun，提高环境变量传递的灵活性
module load python/3.12-conda  # 加载 Python 3.12 conda 环境模块

# 设置工作目录
WORKDIR="/anvme/workspace/b273dd14-swe-openhands/OpenHands"
cd "$WORKDIR"

# ============================================================================
# Conda 环境配置
# ============================================================================
# 设置 Conda 环境和包的存储位置，避免占用 home 目录配额
export CONDA_ENVS_PATH=/anvme/workspace/b273dd14-swe-openhands/conda_envs
export CONDA_PKGS_DIRS=/anvme/workspace/b273dd14-swe-openhands/conda_pkgs
conda activate openhands  # 激活 OpenHands 环境

# 设置错误退出模式：任何命令失败都会终止脚本执行
set -eo pipefail

# 加载版本控制工具函数（包括 checkout_eval_branch、checkout_original_branch 等）
source "/anvme/workspace/b273dd14-swe-openhands/OpenHands/evaluation/utils/version_control.sh"

# ============================================================================
# 数据集与 HuggingFace 配置
# ============================================================================
# 设置默认的本地数据集路径（SWE-rebench leaderboard-2025-06 子集）
# 可通过 SWE_DATASET_LOCAL_PATH 环境变量覆盖此设置
DEFAULT_SWE_DATASET_LOCAL_PATH="/anvme/workspace/b273dd14-swe-openhands/OpenHands/datasets_cache/nebius__SWE-rebench-leaderboard_2025_06_poetry"
export SWE_DATASET_LOCAL_PATH="${SWE_DATASET_LOCAL_PATH:-$DEFAULT_SWE_DATASET_LOCAL_PATH}"
# 可选：替代数据集路径（已注释）
# export SWE_DATASET_LOCAL_PATH=/anvme/workspace/b273dd14-swe-openhands/OpenHands/datasets_cache/princeton-nlp__SWE-bench_Verified

# 启用 HuggingFace 离线模式（避免网络请求），除非显式通过环境变量覆盖
export HF_DATASETS_OFFLINE="${HF_DATASETS_OFFLINE:-1}"

# ============================================================================
# 评估与条件化器配置
# ============================================================================
export EVAL_SKIP_MAXIMUM_RETRIES_EXCEEDED=true  # 跳过达到重试上限的实例，提高效率
# 已注释的选项：可使用 subtask_aware 条件化器（需配置文件支持）
# export EVAL_CONDENSER=subtask_aware
export EVAL_CONDENSER=summarizer_for_eval  # 使用摘要化的评估条件化器

# ============================================================================
# 容器（Apptainer）配置
# ============================================================================
export RUNTIME=apptainer  # 使用 Apptainer（Singularity 的继任者）作为容器运行时
# 定义 Apptainer 缓存目录（存储容器镜像和临时文件）
export APPTAINER_CACHEDIR=/anvme/workspace/b273dd14-swe-openhands/.apptainer_cache
export APPTAINER_TMPDIR=/anvme/workspace/b273dd14-swe-openhands/.apptainer_cache/tmp
export APPTAINER_RUNTIME_LOG_DIR=/anvme/workspace/b273dd14-swe-openhands/.apptainer_cache/logs
# 容器镜像前缀路径
export EVAL_CONTAINER_IMAGE_PREFIX=/anvme/workspace/b273dd14-swe-openhands/.apptainer_cache/images

# ============================================================================
# HuggingFace 与 GPU 配置
# ============================================================================
export HF_HOME="/anvme/workspace/b273dd14-swe-openhands/huggingface_cache"  # HuggingFace 模型和数据缓存位置
export CUDA_VISIBLE_DEVICES=0,1,2,3  # 指定可用 GPU 设备（前 4 块 GPU）
# 代理配置：禁用本地访问的代理，避免不必要的中转
export NO_PROXY="localhost,127.0.0.1"
export no_proxy="localhost,127.0.0.1"

# 创建必要的 Apptainer 缓存目录，防止权限问题
mkdir -p "$APPTAINER_CACHEDIR" "$APPTAINER_TMPDIR"

# ============================================================================
# 模型与日志路径设置
# ============================================================================
# 本地 Qwen3 32B 模型路径（避免每次都从 HuggingFace 下载）
model="/anvme/workspace/b273dd14-swe-openhands/huggingface_cache/Qwen3-32B"
log_dir='logs'  # 日志目录
TIMESTAMP=$(date +%Y%m%d-%H%M%S)  # 时间戳，用于生成唯一的日志文件名

# 模型配置名称与运行标识
DEFAULT_MODEL_CONFIG="llm.eval_qwen3_32b"
run_id="${1:-$DEFAULT_MODEL_CONFIG}"  # 从命令行参数获取，或使用默认值
# 日志文件路径：包含时间戳和运行标识，便于区分不同的运行
vllm_log="$log_dir/vllm_${TIMESTAMP}_${run_id}.log"  # vLLM 服务器日志
swe_log_file="$log_dir/swe_${TIMESTAMP}_${run_id}.log"  # SWE 评估日志

mkdir -p $log_dir  # 创建日志目录


# ============================================================================
# vLLM 服务器端口配置
# ============================================================================
# 支持通过环境变量 VLLM_PORT 指定端口，否则自动分配
if [ -n "$VLLM_PORT" ]; then
    port=$VLLM_PORT
    echo "Using port from environment variable: $port"
else
    # 基于 SLURM_JOB_ID 生成唯一端口（范围：8000-9000）
    # 确保多个并发任务不会争用同一端口
    if [ -n "$SLURM_JOB_ID" ]; then
        port=$((8000 + ($SLURM_JOB_ID % 1000)))
        echo "Generated port from SLURM_JOB_ID: $port"
    else
        # 本地测试时使用默认端口
        port=8003
        echo "Using default port: $port"
    fi

    # 检查端口是否可用，如果不可用则递增查找
    # 使用 ss 命令查询系统中已占用的端口
    while ss -lntu | awk 'NR>1 {print $5}' | sed 's/.*://' | grep -qw "$port"; do
        echo "Port $port is in use, trying next port..."
        port=$((port + 1))
        if [ $port -gt 9000 ]; then
            echo "Error: No available ports in range 8000-9000" >&2
            exit 1
        fi
    done
fi

echo "Port configuration:"
echo "  Unified model $model : $port"



# ============================================================================
# 清理函数与信号处理
# ============================================================================
# 定义清理函数：在脚本退出或被中断时优雅地关闭 vLLM 服务器
cleanup() {
    echo "Script interrupted or exiting. Cleaning up vLLM server..." >&2
    # 检查 vLLM 进程是否仍在运行
    if [ -n "$vllm_pid" ] && ps -p "$vllm_pid" > /dev/null; then
        echo "Stopping vLLM server (PID: $vllm_pid)..." >&2
        kill "$vllm_pid"  # 发送 SIGTERM 信号
        wait "$vllm_pid" 2>/dev/null  # 等待进程完全退出
    fi
    echo "vLLM server stopped." >&2
}

# 为多个信号设置清理函数处理程序
# SIGINT (Ctrl+C)、SIGTERM 以及正常退出时都会触发清理
trap cleanup SIGINT SIGTERM EXIT

# ============================================================================
# 启动统一的 vLLM 服务器
# ============================================================================
# 说明：使用单一 vLLM 服务器同时处理主任务和摘要任务
# 设置最大模型长度为 128k tokens 减去 8k buffer（避免内存溢出）
echo ""
echo "🚀 Starting Unified vLLM Server: $model"

# vLLM 服务器启动命令及配置参数
vllm serve $model \
    --tensor-parallel-size 4 \              # 使用 4 块 GPU 进行张量并行处理
    --reasoning-parser qwen3 \              # 使用 Qwen3 专用的推理解析器
    --enforce-eager \                       # 强制使用 eager 执行模式（避免编译开销）
    --gpu-memory-utilization 0.90 \         # GPU 内存使用率 90%（充分利用显存）
    --enable-auto-tool-choice \             # 启用自动工具选择功能
    --tool-call-parser hermes \             # 工具调用解析器
    --rope-scaling '{"factor": 4.0, "original_max_position_embeddings": 32768, "rope_type": "yarn"}' \  # RoPE 位置编码扩展（支持更长的上下文）
    --enable-prefix-caching \               # 启用前缀缓存，提高重复查询性能
    --max-num-seqs 40 \                     # 最多同时处理 40 个序列
    --max-model-len $((128 * 1024 - 8 * 1024)) \  # 最大模型长度：128k - 8k = 120k tokens
    --seed 41 \                             # 随机种子（保证可重现性）
    --port $port > $vllm_log 2>&1 &         # 监听指定端口，重定向日志输出

vllm_pid=$!  # 保存 vLLM 进程 ID，用于后续的监控和清理

echo "vLLM server starting (PID: $vllm_pid, Port: $port)"

# ============================================================================
# 等待 vLLM 服务器初始化完成
# ============================================================================
timeout_minutes=9  # 初始化超时时间：9 分钟
start_time=$(date +%s)  # 记录启动时间
timeout_seconds=$((timeout_minutes * 60))  # 转换为秒

echo "Waiting for vLLM to initialize (timeout: ${timeout_minutes} minutes)..."

# 定期检查 vLLM 服务器状态，直到初始化完成或超时
while [ $(($(date +%s) - start_time)) -lt $timeout_seconds ]; do
    # 检查 vLLM 进程是否仍在运行
    if ! ps -p $vllm_pid > /dev/null; then
        echo "❌ vLLM server process exited with an error"
        exit 1
    fi

    # 检查日志中是否出现启动完成的标记
    if [ -f "$vllm_log" ] && grep -q "Application startup complete." "$vllm_log"; then
        echo "✅ vLLM initialized successfully"
        break
    fi
    sleep 2  # 每 2 秒检查一次
done

# 判断是否超时
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


MODEL_CONFIG=${1:-$DEFAULT_MODEL_CONFIG}
COMMIT_HASH=${2:-HEAD}
AGENT=${3:-CodeActAgent}
EVAL_LIMIT=${4:-500}
MAX_ITER=${5:-110}
NUM_WORKERS=${6:-1}
DATASET=${7:-nebius/SWE-rebench-leaderboard}
#DATASET=${7:-princeton-nlp/SWE-bench_Verified}
SPLIT=${8:-test}
N_RUNS=${9:-1}
MODE=${10:-swe}
SELECT_FIRST_N=${11:-0}  # 新增参数：选择前 N 个实例（0 表示不限制）

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
  echo "DATASET not specified, use default nebius/SWE-rebench-leaderboard"
  DATASET="nebius/SWE-rebench-leaderboard"
  #echo "DATASET not specified, use default princeton-nlp/SWE-bench_Verified"
  #DATASET="princeton-nlp/SWE-bench_Verified"  echo "DATASET not specified, use default princeton-nlp/SWE-bench_Verified"
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

# Export SELECT_FIRST_N as environment variable for run_infer.py to use
if [ -n "$SELECT_FIRST_N" ]; then
  if [[ "$SELECT_FIRST_N" =~ ^[0-9]+$ ]]; then
    if [ "$SELECT_FIRST_N" -gt 0 ]; then
      export SELECT_FIRST_N
      echo "Will select first $SELECT_FIRST_N instances from the dataset"
    else
      unset SELECT_FIRST_N
      echo "SELECT_FIRST_N is 0 or negative, ignoring explicit limit"
    fi
  elif [[ "$SELECT_FIRST_N" =~ ^[0-9]*:[0-9]*$ ]]; then
    export SELECT_FIRST_N
    echo "Will select dataset slice $SELECT_FIRST_N"
  else
    echo "Invalid SELECT_FIRST_N value: $SELECT_FIRST_N (expected integer or slice start:end). Ignoring."
    unset SELECT_FIRST_N
  fi
else
  echo "No instance selection limit (will use all instances or existing filters)"
fi

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
echo "SELECT_FIRST_N: $SELECT_FIRST_N"

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

  # Create a temporary config file with dynamic port
  TEMP_CONFIG="/tmp/openhands_config_${SLURM_JOB_ID:-$$}_${port}.toml"
  echo "Creating temporary config with dynamic port: $TEMP_CONFIG"

  # Use awk to replace all base_url ports (works on both Linux and macOS)
  awk -v new_port="$port" '
    /^base_url = "http:\/\/localhost:[0-9]+\/v1"/ {
      print "base_url = \"http://localhost:" new_port "/v1\""
      next
    }
    { print }
  ' config.toml > "$TEMP_CONFIG"

  echo "Updated config file with port $port:"
  echo "  [llm.eval_qwen3_32b] base_url -> http://localhost:$port/v1"
  echo "  [llm.condenser_llm] base_url -> http://localhost:$port/v1"

  COMMAND="poetry run python evaluation/benchmarks/swe_bench/run_infer.py \
    --config-file $TEMP_CONFIG \
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

  # Clean up temporary config
  rm -f "$TEMP_CONFIG"
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
