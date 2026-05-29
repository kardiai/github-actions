import argparse
import sys
import io
import math
import requests

import boto3
from concurrent.futures import ThreadPoolExecutor
from check_build_before_release import read_yaml
from check_build_before_release import get_aws_session

BASE_ECR_REGION = "eu-central-1"
BASE_ECR_ACCOUNT = "516990111628"

def ensure_ecr_repository(repo_name, region, account):
    ecr = boto3.client('ecr', region_name=region)
    try:
        if account != BASE_ECR_ACCOUNT:
            ecr.describe_repositories(repositoryNames=[repo_name], registryId=account)
        else:
            ecr.describe_repositories(repositoryNames=[repo_name])
    except ecr.exceptions.RepositoryNotFoundException:
        ecr.create_repository(repositoryName=repo_name, registryId=account)

def ecr_image_exists(repo_name, tag, region, account):
    ecr = boto3.client('ecr', region_name=region)
    try:
        if account != BASE_ECR_ACCOUNT:
            ecr.describe_images(repositoryName=repo_name,
                                imageIds=[{'imageTag': tag}],
                                registryId=account)
        else:
            ecr.describe_images(repositoryName=repo_name,
                                imageIds=[{'imageTag': tag}])
        return True
    except ecr.exceptions.ImageNotFoundException:
        return False
    except Exception as e:
        print(f"Error checking image existence {repo_name}:{tag} in {region}: {e}")
        sys.exit(1)

def copy_ecr_image_if_missing(repo_name, tag, src_region, dst_region, dst_account, dry_run=False):
    if ecr_image_exists(repo_name, tag, dst_region, dst_account):
        return None  # already present

    source_ecr = boto3.client('ecr', region_name=src_region)
    target_ecr = boto3.client('ecr', region_name=dst_region)

    try:
        print(f"Checking source image {repo_name}:{tag} in {src_region}...")
        resp = source_ecr.batch_get_image(
            repositoryName=repo_name,
            imageIds=[{'imageTag': tag}],
            registryId=BASE_ECR_ACCOUNT,
            acceptedMediaTypes=[
                'application/vnd.docker.distribution.manifest.v2+json',
                'application/vnd.oci.image.manifest.v1+json'
            ]
        )
        images = resp.get('images', [])
        if not images:
            print(f"Source image not found in {src_region}: {repo_name}:{tag}")
            return None

        image_manifest = images[0]['imageManifest']
        media_type = images[0].get('imageManifestMediaType')

        ensure_ecr_repository(repo_name, dst_region, dst_account)

        if dry_run:
            return f"Dry run: Would copy {repo_name}:{tag} from {src_region} to {dst_region}"

        import json
        manifest_obj = json.loads(image_manifest)

        # print("manifest keys:", manifest_obj.keys())
        # if "manifests" in manifest_obj:
        #     print("This is a manifest list with entries:", [m.get("platform") for m in manifest_obj["manifests"]])
        # if "layers" in manifest_obj:
        #     layer_digests = [layer.get("digest") for layer in manifest_obj["layers"]]
        #     print("layer_digests from manifest:", layer_digests)
        # else:
        #     print("No 'layers' in manifest, manifest is probably an index/manifest list")

        layer_digests = [layer["digest"] for layer in manifest_obj.get("layers", [])]
        config_digest = manifest_obj.get("config", {}).get("digest")
        if config_digest:
            layer_digests.insert(0, config_digest)


        check = target_ecr.batch_check_layer_availability(
            registryId=dst_account,
            repositoryName=repo_name,
            layerDigests=layer_digests,
        )

        # print("batch_check response layers:", check.get("layers"))
        # print("batch_check response failures:", check.get("failures"))

        available = {
            layer.get("layerDigest")
            for layer in check.get("layers", [])
            if layer.get("layerAvailability") == "AVAILABLE" and layer.get("layerDigest")
        }
        missing = [d for d in layer_digests if d not in available]

        for digest in missing:
            # Get a download URL from the source (requires permission to access source)
            dl = source_ecr.get_download_url_for_layer(
                registryId=BASE_ECR_ACCOUNT, repositoryName=repo_name, layerDigest=digest
            )
            url = dl["downloadUrl"]

            # Download the blob (stream)
            r = requests.get(url, stream=True)
            r.raise_for_status()

            # Read entire blob into memory or stream into parts.
            # For large layers you should stream and upload in parts.
            blob = r.content
            total_bytes = len(blob)

            # Initiate upload on destination
            init = target_ecr.initiate_layer_upload(registryId=dst_account, repositoryName=repo_name)
            upload_id = init["uploadId"]
            # The API allows UploadLayerPart with byte range offsets; choose a part size.
            part_size = 8 * 1024 * 1024  # 8 MiB (tune as needed)
            part_count = math.ceil(total_bytes / part_size)

            for i in range(part_count):
                start = i * part_size
                end = min(total_bytes, start + part_size)
                part_bytes = blob[start:end]
                # Upload a part
                target_ecr.upload_layer_part(
                    registryId=dst_account,
                    repositoryName=repo_name,
                    uploadId=upload_id,
                    partFirstByte=start,
                    partLastByte=end - 1,
                    layerPartBlob=part_bytes,
                )

            # After all parts uploaded, complete the layer upload with the digest
            target_ecr.complete_layer_upload(
                registryId=dst_account,
                repositoryName=repo_name,
                uploadId=upload_id,
                layerDigests=[digest],
            )

        put_kwargs = {
            'repositoryName': repo_name,
            'imageManifest': image_manifest,
            'imageTag': tag
        }
        if media_type:
            put_kwargs['imageManifestMediaType'] = media_type
        if dst_account:
            put_kwargs['registryId'] = dst_account

        # push the manifest with the original tag
        target_ecr.put_image(**put_kwargs)

        try:
            print(f"Removing latest tag for {repo_name} in {dst_region}...")
            del_resp = target_ecr.batch_delete_image(
                registryId=dst_account,
                repositoryName=repo_name,
                imageIds=[{"imageTag": "latest"}],
            )
            # print("delete response:", del_resp)
        except Exception as exc:
            print("'Latest' tag removal failed:", exc)
            raise

        # also tag the same manifest as "latest" in the destination registry
        latest_kwargs = {
            'repositoryName': repo_name,
            'imageManifest': image_manifest,
            'imageTag': 'latest'
        }
        if media_type:
            latest_kwargs['imageManifestMediaType'] = media_type
        if dst_account:
            latest_kwargs['registryId'] = dst_account

        # put_image with the same manifest but imageTag='latest' will create/update the 'latest' tag
        target_ecr.put_image(**latest_kwargs)
        return f"Copied {repo_name}:{tag} (and tagged as latest) from {src_region} to {dst_region}"
    except Exception as e:
        print(f"Error copying image {repo_name}:{tag} from {src_region} to {dst_region}: {e}")
        sys.exit(1)

def ensure_target_region_images(yaml_file, target_region, target_account, dry_run=False):
    lambdas = read_yaml(yaml_file)['lambdas']
    messages = []
    for l in lambdas:
        repo = l['name']
        tag = l['version']
        msg = copy_ecr_image_if_missing(repo, tag, BASE_ECR_REGION, target_region, target_account, dry_run=dry_run)
        if msg:
            messages.append(msg)
    if messages:
        print("ECR copy summary:")
        for m in messages:
            print(m)

def get_current_image(function_name, region):
    client = boto3.client('lambda', region_name=region)
    try:
        resp = client.get_function(FunctionName=function_name)
        return resp['Code']['ImageUri']
    except Exception as e:
        print(f"Error getting function {function_name}: {e}")
        sys.exit(1)

def update_lambda(function_name, image_uri, region, dry_run=False, current_image=None):
    # get image versions from image_uri and current_image to be able to log the update
    if current_image:
        current_version = current_image.split(':')[-1]
    else:
        print("Can't read current image URI.")
        sys.exit(1)

    if image_uri:
        new_version = image_uri.split(':')[-1]
    else:
        print("Can't read new deployment image URI")
        sys.exit(1)

    if dry_run:
        print(f"Dry run: Update {function_name} from {current_version} to {new_version}")

    client = boto3.client('lambda', region_name=region)
    try:
        client.update_function_code(
            FunctionName=function_name,
            Architectures=['arm64'],
            ImageUri=image_uri,
            DryRun=dry_run
        )
        return f"Update {function_name} from {current_version} to {new_version}"
    except Exception as e:
        print(f"Error updating {function_name}: {e}")
        sys.exit(1)

def process_lambdas(yaml_file, suffix, account, region, log_file, dry_run=False):
    if region != BASE_ECR_REGION:
        print(f"Ensuring images exist in target region {region} (copying from {BASE_ECR_REGION} if missing)...")
        ensure_target_region_images(yaml_file, region, account, dry_run=dry_run)

    lambdas = read_yaml(yaml_file)['lambdas']
    updates = []
    with ThreadPoolExecutor() as executor:
        futures = []
        for l in lambdas:
            name = l['name'] + suffix
            version = l['version']
            new_image = f"{account}.dkr.ecr.{region}.amazonaws.com/{l['name']}:{version}"
            current_image = get_current_image(name, region)
            # print(f"Checking {name} in {region} for version {version} with image {new_image}")
            if current_image != new_image:
                futures.append(executor.submit(update_lambda, name, new_image, region, dry_run, current_image))
        for future in futures:
            result = future.result()
            if result:
                updates.append(result)
    if len(updates) > 0 and not dry_run:
        with open(log_file, 'a') as log:
            for u in updates:
                log.write(u + '\n')
        #print out updates array as text
        print(f"{len(updates)} lambdas updated:")
        for u in updates:
            print(u)
    elif len(updates) > 0 and dry_run:
        print("Dry run mode, no lambdas updated.")
    else:
        print("All versions are up to date, no updates needed.")

def main():

    parser = argparse.ArgumentParser(description="Deploy or update AWS Lambda functions from a YAML file.",
                                     epilog="Example: python3 deploy_lambdas.py --yaml-file lambdas.yaml --suffix '' --account 1234567890 --region eu-central-1 --log-file modified_lambdas.log --dry-run")
    parser.add_argument('--yaml-file', required=True, help='Path to the YAML file with lambda definitions')
    parser.add_argument('--suffix', default="", help='Suffix to append to lambda names')
    parser.add_argument('--account', default='516990111628', help='AWS account ID')
    parser.add_argument('--region', default='eu-central-1', help='AWS region')
    parser.add_argument('--log-file', default='modified_lambdas.log', help='Log file for updated lambdas')
    parser.add_argument('--dry-run', action='store_true', help='Perform a dry run without updating lambdas')

    args = parser.parse_args()

    print(f"Using AWS account: {args.account}, region: {args.region}, suffix: {args.suffix}, dry-run: {args.dry_run}")
    get_aws_session()

    print(f"Processing YAML file: {args.yaml_file}")
    process_lambdas(
        args.yaml_file,
        args.suffix,
        args.account,
        args.region,
        args.log_file,
        args.dry_run
    )

if __name__ == "__main__":
    main()
