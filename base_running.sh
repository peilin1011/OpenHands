source ~/.bashrc

# 或者手动添加 Node.js 到 PATH
export PATH="$HOME/local/nodejs/node-v22.11.0-linux-x64/bin:$PATH"

export RUNTIME=local
  ./evaluation/benchmarks/swe_bench/scripts/run_infer.sh llm.eval_gpt5 HEAD CodeActAgent 1 30 1 princeton-nlp/SWE-bench_Verified test

export http_proxy=http://proxy.nhr.fau.de:80
export https_proxy=http://proxy.nhr.fau.de:80

export CONDA_ENVS_PATH=/anvme/workspace/b273dd14-swe-openhands/conda_envs
export CONDA_PKGS_DIRS=/anvme/workspace/b273dd14-swe-openhands/conda_pkgs

module load python/3.12-conda
conda activate op312