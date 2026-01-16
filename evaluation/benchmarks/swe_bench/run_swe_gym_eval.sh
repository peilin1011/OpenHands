#!/bin/bash

################################################################################
# SWE Gym 评估脚本 - 使用预构建 Apptainer 镜像
################################################################################

set -e

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 配置
PROJECT_ROOT="/anvme/workspace/b273dd14-swe-openhands"
CACHE_DIR="$PROJECT_ROOT/.apptainer_cache"
WORKSPACE="$PROJECT_ROOT/OpenHands"
INPUT_FILE="${1:-$PROJECT_ROOT/output.jsonl}"
DATASET="${2:-swegym/SWE-Gym}"
NUM_WORKERS="${3:-1}"

# 首先切换到项目根目录（必须！）
cd "$PROJECT_ROOT"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}SWE Gym 评估工具${NC}"
echo -e "${BLUE}========================================${NC}"

# 验证输入文件（使用绝对路径）
if [ ! -f "$INPUT_FILE" ]; then
    echo -e "${RED}✗ 错误: 找不到输入文件 '$INPUT_FILE'${NC}"
    echo "用法: $0 <predictions.jsonl> [dataset] [num_workers]"
    echo "当前工作目录: $(pwd)"
    exit 1
fi

echo -e "${GREEN}✓ 工作目录: $(pwd)${NC}"
echo -e "${GREEN}✓ 输入文件: $INPUT_FILE${NC}"

# 验证缓存目录
if [ ! -d "$CACHE_DIR/images" ]; then
    echo -e "${RED}✗ 错误: 缓存目录不存在 '$CACHE_DIR/images'${NC}"
    exit 1
fi

SIFS_COUNT=$(ls $CACHE_DIR/images/*.sif 2>/dev/null | wc -l)
CACHE_SIZE=$(du -sh $CACHE_DIR/images 2>/dev/null | cut -f1)

echo -e "${GREEN}✓ Apptainer 缓存: $CACHE_DIR${NC}"
echo -e "${GREEN}✓ 预构建镜像数量: $SIFS_COUNT${NC}"
echo -e "${GREEN}✓ 缓存大小: $CACHE_SIZE${NC}"

# 验证 Apptainer 已安装
if ! command -v apptainer &> /dev/null; then
    echo -e "${YELLOW}⚠ 警告: apptainer 命令未找到${NC}"
    echo "请安装 Apptainer 或加载模块："
    echo "  apt-get install -y apptainer  (Ubuntu/Debian)"
    echo "  module load apptainer  (HPC 环境)"
fi

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}配置参数${NC}"
echo -e "${BLUE}========================================${NC}"
echo -e "数据集: ${YELLOW}$DATASET${NC}"
echo -e "工作进程数: ${YELLOW}$NUM_WORKERS${NC}"
echo -e "运行时: ${YELLOW}apptainer${NC}"
echo ""

# 设置环境变量
export RUNTIME=apptainer
export APPTAINER_CACHEDIR=$CACHE_DIR

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}开始评估${NC}"
echo -e "${BLUE}========================================${NC}"

cd "$WORKSPACE"

# 显示命令
echo -e "${YELLOW}运行命令:${NC}"
echo "python evaluation/benchmarks/swe_bench/eval_infer.py \\"
echo "    --input-file $INPUT_FILE \\"
echo "    --dataset $DATASET \\"
echo "    --eval-num-workers $NUM_WORKERS"
echo ""

# 运行评估
python evaluation/benchmarks/swe_bench/eval_infer.py \
    --input-file "$INPUT_FILE" \
    --dataset "$DATASET" \
    --eval-num-workers "$NUM_WORKERS"

# 显示结果
OUTPUT_FILE="${INPUT_FILE%.jsonl}.swebench_eval.jsonl"
if [ -f "$OUTPUT_FILE" ]; then
    echo ""
    echo -e "${BLUE}========================================${NC}"
    echo -e "${GREEN}✓ 评估完成！${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo -e "结果文件: ${YELLOW}$OUTPUT_FILE${NC}"
    echo "结果摘要:"
    grep -o '"resolved": [^,}]*' "$OUTPUT_FILE" | sort | uniq -c || true
else
    echo -e "${RED}✗ 结果文件未找到${NC}"
    exit 1
fi
