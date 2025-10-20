# Building OpenHands + SWE-Bench Runtime Images for Apptainer

This guide describes how to produce container images that bundle the OpenHands
runtime with SWE-Bench instance environments. You can build these images on a
machine with Docker support and then convert them to Apptainer/Singularity
(`.sif`) files for HPC environments where Docker is unavailable.

## Overview

OpenHands' evaluation pipeline expects a container image that contains:

1. The OpenHands runtime stack (micromamba, poetry, action-execution server,
   etc.), and
2. The SWE-Bench instance repository and all of its dependencies.

The Docker implementation assembles these layers on the fly. On systems without
Docker (e.g., many HPC clusters), you must build the combined image elsewhere
and then pull it with Apptainer.

## Files

- `scripts/apptainer/Dockerfile.openhands-swebench`  
  Multi-stage Dockerfile that merges an OpenHands runtime image with a specific
  SWE-Bench instance image.

- `scripts/apptainer/build_openhands_swe_image.sh`  
  Helper script that wraps `docker build`. Usage:
  ```bash
  ./scripts/apptainer/build_openhands_swe_image.sh \
    swebench/sweb.eval.x86_64.scikit-learn_1776_scikit-learn-13439:latest \
    myrepo/openhands-swebench:scikit-learn
  ```

- `scripts/apptainer/build_all_instances.sh`  
  Example batch script; edit the `INSTANCES` array with the images you need. It
  will tag each output as `<repo>:<instance-name>`.

## Step-by-Step Instructions

1. **Choose a machine with Docker access.**

2. **Build the combined Docker image.**
   ```bash
   ./scripts/apptainer/build_openhands_swe_image.sh \
     swebench/sweb.eval.x86_64.scikit-learn_1776_scikit-learn-13439:latest \
     myrepo/openhands-swebench:scikit-learn
   ```

3. **(Optional) Push the image to a registry.**
   ```bash
   docker push myrepo/openhands-swebench:scikit-learn
   ```

4. **On the HPC system, pull and convert to Apptainer.**
   ```bash
   export APPTAINER_CACHEDIR=/path/to/cache
   export APPTAINER_TMPDIR=/path/to/tmp
   mkdir -p "$APPTAINER_CACHEDIR" "$APPTAINER_TMPDIR"

   apptainer pull /path/to/openhands-swebench.sif \
     docker://myrepo/openhands-swebench:scikit-learn
   ```

5. **Point OpenHands to the `.sif` file.**
   In `config.toml`:
   ```toml
   [sandbox]
   runtime = "apptainer"
   runtime_container_image = "/path/to/openhands-swebench.sif"
   ```
   Ensure Apptainer environment variables are set before running:
   ```bash
   export RUNTIME=apptainer
   export APPTAINER_CACHEDIR=/path/to/cache
   export APPTAINER_TMPDIR=/path/to/tmp
   export APPTAINER_RUNTIME_LOG_DIR=/path/to/logs
   ```

6. **Run the evaluation script.**
   ```bash
   ./evaluation/benchmarks/swe_bench/scripts/run_infer.sh \
     llm.eval_gpt5 HEAD CodeActAgent 1 30 1 \
     princeton-nlp/SWE-bench_Verified test
   ```

## Notes

- The default OpenHands runtime image is `ghcr.io/all-hands-ai/runtime:latest`.
  You can override it via the third argument to
  `build_openhands_swe_image.sh`.
- Multiple `.sif` files can be produced—one per SWE-Bench instance—or you can
  generate a single multi-instance image by preloading several repositories.
- Keep an eye on storage quotas. SWE-Bench images are large (~1 GB each).
- For HPC systems using Apptainer in unprivileged mode, you may need to use
  `apptainer build --fakeroot` when creating `.sif` files directly on-cluster.
