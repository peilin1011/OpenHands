"""
Build composite Docker images for SWE-bench instances and push to Docker Hub.

This script builds OpenHands runtime images based on SWE-bench official images,
then pushes them to Docker Hub for later use in Apptainer environments.

Usage:
    python evaluation/benchmarks/swe_bench/scripts/build_and_push_images.py \
        --dataset princeton-nlp/SWE-bench_Verified \
        --split test \
        --dockerhub-user yourname \
        --dockerhub-repo openhands-swebench \
        --slice 100:200
"""

import argparse
import os
import sys
from pathlib import Path

import docker
import pandas as pd
from datasets import load_dataset, load_from_disk

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent.parent))

from evaluation.benchmarks.swe_bench.run_infer import (
    get_instance_docker_image,
    set_dataset_type,
    filter_dataset,
)
from openhands.core.logger import openhands_logger as logger
from openhands.runtime.builder import DockerRuntimeBuilder
from openhands.runtime.utils.runtime_build import build_runtime_image


def parse_args():
    parser = argparse.ArgumentParser(
        description='Build and push composite SWE-bench images to Docker Hub'
    )
    parser.add_argument(
        '--dataset',
        type=str,
        default='princeton-nlp/SWE-bench_Verified',
        help='Dataset to load instances from',
    )
    parser.add_argument(
        '--split',
        type=str,
        default='test',
        help='Dataset split to use',
    )
    parser.add_argument(
        '--dockerhub-user',
        type=str,
        required=True,
        help='Docker Hub username',
    )
    parser.add_argument(
        '--dockerhub-repo',
        type=str,
        default='openhands-swebench',
        help='Docker Hub repository name (default: openhands-swebench)',
    )
    parser.add_argument(
        '--slice',
        type=str,
        default=None,
        help='Slice of instances to build, format: "start:end" (e.g., "100:200" for instances 100-199)',
    )
    parser.add_argument(
        '--platform',
        type=str,
        default='linux/amd64',
        help='Target platform (default: linux/amd64)',
    )
    parser.add_argument(
        '--enable-browser',
        action='store_true',
        default=False,
        help='Enable browser support (installs Playwright)',
    )
    parser.add_argument(
        '--force-rebuild',
        action='store_true',
        default=False,
        help='Force rebuild even if image exists',
    )
    parser.add_argument(
        '--skip-push',
        action='store_true',
        default=False,
        help='Skip pushing to Docker Hub (build only)',
    )
    parser.add_argument(
        '--selected-ids',
        type=str,
        default=None,
        help='Comma-separated list of instance IDs to build',
    )
    return parser.parse_args()


def generate_image_name(instance_id: str, dockerhub_user: str, dockerhub_repo: str) -> str:
    """
    Generate Docker Hub image name from instance ID.

    Follows the EXACT naming convention used in run_infer.py:
    sweb.eval.x86_64.{instance_id with __ replaced by _s_}

    This matches the .sif filename that will be automatically detected:
    /path/.apptainer_cache/images/sweb.eval.x86_64.scikit-learn_s_scikit-learn-25500.sif

    Args:
        instance_id: e.g., "scikit-learn__scikit-learn-25500"
        dockerhub_user: Docker Hub username
        dockerhub_repo: Docker Hub repository name

    Returns:
        Full image name: "user/repo:sweb.eval.x86_64.scikit-learn_s_scikit-learn-25500"
    """
    # Convert instance_id to match Apptainer .sif naming
    # This follows run_infer.py line 226-229:
    # image_name = 'sweb.eval.x86_64.' + instance_id
    # image_name = image_name.replace('__', '_s_')
    image_tag = f"sweb.eval.x86_64.{instance_id.replace('__', '_s_')}"

    return f"{dockerhub_user}/{dockerhub_repo}:{image_tag}"


def build_instance_image(
    instance_id: str,
    base_image: str,
    target_image: str,
    docker_builder: DockerRuntimeBuilder,
    platform: str = 'linux/amd64',
    enable_browser: bool = False,
    force_rebuild: bool = False,
) -> str:
    """
    Build a composite runtime image for a SWE-bench instance.

    Args:
        instance_id: Instance ID
        base_image: Base SWE-bench official image
        target_image: Target image name with tag
        docker_builder: Docker runtime builder
        platform: Target platform
        enable_browser: Whether to enable browser support
        force_rebuild: Whether to force rebuild

    Returns:
        Built image name
    """
    logger.info(f'Building image for instance: {instance_id}')
    logger.info(f'  Base image: {base_image}')
    logger.info(f'  Target: {target_image}')

    # Check if target image already exists
    if not force_rebuild:
        try:
            if docker_builder.image_exists(target_image, pull_from_repo=False):
                logger.info(f'  Image already exists locally: {target_image}')
                return target_image
        except Exception as e:
            logger.debug(f'  Error checking image existence: {e}')

    # Build the runtime image
    try:
        built_image = build_runtime_image(
            base_image=base_image,
            runtime_builder=docker_builder,
            platform=platform,
            enable_browser=enable_browser,
            force_rebuild=force_rebuild,
        )
        logger.info(f'  Built image: {built_image}')
        return built_image
    except Exception as e:
        logger.error(f'  Failed to build image for {instance_id}: {e}')
        raise


def tag_and_push_image(
    docker_client: docker.DockerClient,
    source_image: str,
    target_image: str,
    skip_push: bool = False,
) -> bool:
    """
    Tag and push image to Docker Hub.

    Args:
        docker_client: Docker client
        source_image: Source image name (built image)
        target_image: Target image name (Docker Hub name)
        skip_push: Whether to skip pushing

    Returns:
        True if successful, False otherwise
    """
    try:
        # Parse target image
        if ':' in target_image:
            repo, tag = target_image.split(':', 1)
        else:
            repo = target_image
            tag = 'latest'

        # Get source image
        image = docker_client.images.get(source_image)

        # Tag image
        logger.info(f'  Tagging: {source_image} -> {target_image}')
        image.tag(repo, tag)

        if skip_push:
            logger.info(f'  Skipping push (--skip-push enabled)')
            return True

        # Push to Docker Hub
        logger.info(f'  Pushing to Docker Hub: {target_image}')
        for line in docker_client.images.push(repo, tag, stream=True, decode=True):
            if 'status' in line:
                status = line['status']
                if 'id' in line:
                    logger.debug(f"    {line['id']}: {status}")
                elif status not in ['Preparing', 'Waiting', 'Layer already exists']:
                    logger.info(f'    {status}')
            if 'error' in line:
                logger.error(f"    Error: {line['error']}")
                return False

        logger.info(f'  ✓ Successfully pushed: {target_image}')
        return True

    except Exception as e:
        logger.error(f'  Failed to tag/push image: {e}')
        return False


def main():
    args = parse_args()

    # Initialize Docker client
    try:
        docker_client = docker.from_env()
        docker_builder = DockerRuntimeBuilder(docker_client)
    except Exception as e:
        logger.error(f'Failed to initialize Docker client: {e}')
        logger.error('Please make sure Docker is running.')
        sys.exit(1)

    # Load dataset
    logger.info('=' * 80)
    logger.info(f'Loading dataset: {args.dataset} (split: {args.split})')
    logger.info('=' * 80)

    local_dataset_path = os.environ.get('SWE_DATASET_LOCAL_PATH')

    if local_dataset_path:
        logger.info(f'Loading from local path: {local_dataset_path}')
        dataset = load_from_disk(local_dataset_path)
        if hasattr(dataset, args.split):
            dataset = dataset[args.split]
    else:
        dataset = load_dataset(args.dataset, split=args.split)

    # Set dataset type
    set_dataset_type(args.dataset)

    # Filter dataset
    instances = filter_dataset(dataset.to_pandas(), 'instance_id')

    # Apply selected IDs filter if provided
    if args.selected_ids:
        selected_ids = [id.strip() for id in args.selected_ids.split(',')]
        instances = instances[instances['instance_id'].isin(selected_ids)]
        logger.info(f'Filtered to {len(instances)} selected instances')

    # Apply slice
    if args.slice:
        try:
            slice_parts = args.slice.split(':')
            if len(slice_parts) != 2:
                raise ValueError("Slice must be in format 'start:end'")
            start = int(slice_parts[0]) if slice_parts[0] else 0
            end = int(slice_parts[1]) if slice_parts[1] else len(instances)
            instances = instances.iloc[start:end]
            logger.info(f'Sliced to instances [{start}:{end}], total: {len(instances)}')
        except Exception as e:
            logger.error(f'Invalid slice format: {args.slice}. Use format "start:end" (e.g., "100:200")')
            sys.exit(1)

    logger.info(f'Total instances to process: {len(instances)}')
    logger.info('=' * 80)

    # Build and push images
    successful = []
    failed = []
    skipped = []

    for idx, (_, instance) in enumerate(instances.iterrows(), 1):
        instance_id = instance['instance_id']

        logger.info('')
        logger.info(f'[{idx}/{len(instances)}] Processing: {instance_id}')
        logger.info('-' * 80)

        try:
            # Get base SWE-bench official image
            base_image = get_instance_docker_image(
                instance_id=instance_id,
                swebench_official_image=True,
            )

            # Generate target image name
            target_image = generate_image_name(
                instance_id=instance_id,
                dockerhub_user=args.dockerhub_user,
                dockerhub_repo=args.dockerhub_repo,
            )

            # Build composite image
            built_image = build_instance_image(
                instance_id=instance_id,
                base_image=base_image,
                target_image=target_image,
                docker_builder=docker_builder,
                platform=args.platform,
                enable_browser=args.enable_browser,
                force_rebuild=args.force_rebuild,
            )

            # Tag and push to Docker Hub
            push_success = tag_and_push_image(
                docker_client=docker_client,
                source_image=built_image,
                target_image=target_image,
                skip_push=args.skip_push,
            )

            if push_success:
                successful.append({
                    'instance_id': instance_id,
                    'image': target_image,
                })
            else:
                failed.append({
                    'instance_id': instance_id,
                    'error': 'Push failed',
                })

        except Exception as e:
            logger.error(f'  Error processing {instance_id}: {e}')
            failed.append({
                'instance_id': instance_id,
                'error': str(e),
            })

    # Summary
    logger.info('')
    logger.info('=' * 80)
    logger.info('BUILD AND PUSH SUMMARY')
    logger.info('=' * 80)
    logger.info(f'Total instances: {len(instances)}')
    logger.info(f'Successful: {len(successful)}')
    logger.info(f'Failed: {len(failed)}')
    logger.info(f'Skipped: {len(skipped)}')

    if successful:
        logger.info('')
        logger.info('Successfully built and pushed:')
        for item in successful:
            logger.info(f"  ✓ {item['instance_id']}")
            logger.info(f"    → {item['image']}")

    if failed:
        logger.info('')
        logger.info('Failed:')
        for item in failed:
            logger.info(f"  ✗ {item['instance_id']}: {item['error']}")

    logger.info('=' * 80)

    # Generate Apptainer pull commands
    if successful and not args.skip_push:
        logger.info('')
        logger.info('Apptainer pull commands for HPC:')
        logger.info('=' * 80)
        logger.info('#!/bin/bash')
        logger.info('# Pull all images on HPC')
        logger.info('')
        logger.info('SIF_DIR="/path/to/sif/files"')
        logger.info('mkdir -p "$SIF_DIR"')
        logger.info('')
        for item in successful:
            instance_id = item['instance_id']
            image = item['image']
            # Extract tag to use as .sif filename
            tag = image.split(':')[1] if ':' in image else 'latest'
            logger.info(f'# {instance_id}')
            logger.info(f'apptainer pull "$SIF_DIR/{tag}.sif" docker://{image}')
            logger.info('')
        logger.info('=' * 80)

    # Exit with error if any failed
    if failed:
        sys.exit(1)


if __name__ == '__main__':
    main()
