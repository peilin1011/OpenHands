source ~/.bashrc

# 或者手动添加 Node.js 到 PATH
export PATH="$HOME/local/nodejs/node-v22.11.0-linux-x64/bin:$PATH"

export RUNTIME=

export http_proxy=http://proxy.nhr.fau.de:80
export https_proxy=http://proxy.nhr.fau.de:80

# Set Apptainer specific proxy settings
export APPTAINER_HTTP_PROXY=http://proxy.nhr.fau.de:80
export APPTAINER_HTTPS_PROXY=http://proxy.nhr.fau.de:80



———————————————————— Run ————————————
module load python/3.12-conda

export CONDA_ENVS_PATH=/anvme/workspace/b273dd14-swe-openhands/conda_envs
export CONDA_PKGS_DIRS=/anvme/workspace/b273dd14-swe-openhands/conda_pkgs
conda activate openhands

export RUNTIME=apptainer
export APPTAINER_CACHEDIR=/anvme/workspace/b273dd14-swe-openhands/cache/apptainer
export APPTAINER_TMPDIR=/anvme/workspace/b273dd14-swe-openhands/cache/apptainer/tmp
export APPTAINER_RUNTIME_LOG_DIR=/anvme/workspace/b273dd14-swe-openhands/cache/apptainer/logs
export EVAL_CONTAINER_IMAGE_PREFIX=/anvme/workspace/b273dd14-swe-openhands/.apptainer_cache/images


./evaluation/benchmarks/swe_bench/scripts/run_infer.sh   llm.eval_gpt5 HEAD CodeActAgent 1 40 1   princeton-nlp/SWE-bench_Lite test


git commit -m "ADD already generate patch" --no-verify


# 任务1：实例 1-10
sbatch qwen32b_job.sh llm.eval_qwen3_32b HEAD CodeActAgent 500 110 1 \
    princeton-nlp/SWE-bench_Verified test 1 swe 10

# 任务2：实例 11-20
export SKIP_IDS="..."  # 前10个实例的ID
sbatch qwen32b_job.sh ... 20

# 任务3：实例 21-30
sbatch qwen32b_job.sh ... 30
