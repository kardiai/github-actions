import os
import sys
import subprocess
import json
import yaml
from concurrent.futures import ThreadPoolExecutor
import boto3
from botocore.exceptions import ClientError
import threading
import shutil

STEP_FUNCTION_CONFIG_DIR = "step-functions-config"
INFERENCE_CONFIG_FILE = "inference-config/lambda.yml"
BUCKET_NAME = os.path.dirname(os.getenv("STEP_FUNCTION_CONFIG_BUCKET_NAME", ""))

_print_lock = threading.Lock()

def safe_print(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)

def get_aws_session():
    if os.getenv("AWS_PROFILE"):
        return boto3.Session(
            profile_name=os.getenv("AWS_PROFILE"),
            region_name=os.getenv("AWS_REGION")
        )
    if os.getenv("AWS_ACCESS_KEY_ID"):
        if not os.getenv("AWS_SECRET_ACCESS_KEY") or not os.getenv("AWS_REGION"):
            raise EnvironmentError(
                "AWS_ACCESS_KEY_ID is set but missing AWS_SECRET_ACCESS_KEY or AWS_REGION"
            )
        return boto3.Session(
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=os.getenv("AWS_REGION")
        )
    return boto3.Session()

def read_json(file_path):
    try:
        with open(file_path, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse JSON: {file_path}") from e

def read_yaml(file_path):
    try:
        with open(file_path, 'r') as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValueError(f"Failed to parse YAML: {file_path}") from e


EXTRA_MODEL_SYNC_PATHS = {
    "heartbeat_classification": ["trained-models/point-classification"],
    "af_classification": ["trained-models/af-detection"],
}


def check_model_versions():
    print("Checking presence of model versions in S3...")
    print(f"If model is missing in S3 bucket {BUCKET_NAME}, it will be copied from S3 bucket {os.getenv('MODEL_STORE_BUCKET')}")

    aws_cli_path = shutil.which("aws")
    if not aws_cli_path:
        print(f"Warning: AWS CLI not found in PATH. Cannot sync models between environments.")
        print(f'Debug: OS PATH: {os.getenv("PATH")}')
    else:
        print(f"Debug: aws_cli_path: {aws_cli_path}\n")

    get_aws_session()
    s3_client = boto3.client('s3')
    inference_config = read_yaml(INFERENCE_CONFIG_FILE)
    model_copied = set()
    model_copy_lock = threading.Lock()

    def path_exists_in_s3(model_path, model_version):
        s3_prefix = f"{model_path}/{model_version}/"
        try:
            response = s3_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=s3_prefix, MaxKeys=1)
            return 'Contents' in response
        except ClientError as e:
            raise RuntimeError(f"Unexpected S3 error for s3://{BUCKET_NAME}/{s3_prefix}: {e}") from e

    def copy_from_model_store(inference_name, model_path, model_version):
        model_key = f"{model_path}/{model_version}"
        with model_copy_lock:
            if model_key in model_copied:
                safe_print(f"  {inference_name} [{model_path}] - Already copied from model store. Skipping.")
                return
            safe_print(f"  Model {model_key} missing in {BUCKET_NAME}, copying from {os.getenv('MODEL_STORE_BUCKET')}")
            model_copied.add(model_key)

        MODEL_STORE_AWS_ACCOUNT = os.getenv("MODEL_STORE_AWS_ACCOUNT")
        MODEL_STORE_AWS_REGION = os.getenv("MODEL_STORE_AWS_REGION")
        MODEL_STORE_BUCKET = os.getenv("MODEL_STORE_BUCKET")
        if not MODEL_STORE_AWS_ACCOUNT or not MODEL_STORE_AWS_REGION or not MODEL_STORE_BUCKET:
            raise EnvironmentError(
                "Missing required env vars: MODEL_STORE_AWS_ACCOUNT, MODEL_STORE_AWS_REGION, MODEL_STORE_BUCKET"
            )
        if not aws_cli_path:
            raise RuntimeError("AWS CLI not found in PATH — cannot sync models")

        src = f"s3://{MODEL_STORE_BUCKET}/{model_path}/{model_version}/"
        dst = f"s3://{BUCKET_NAME}/{model_path}/{model_version}/"
        try:
            result = subprocess.run(
                [aws_cli_path, "s3", "sync", src, dst, "--source-region", MODEL_STORE_AWS_REGION],
                check=True
            )
            safe_print(f"  {result}")
        except Exception as e:
            with model_copy_lock:
                model_copied.discard(model_key)
            raise RuntimeError(f"Failed to sync model {model_key} from model store: {e}") from e

    def process_config_file(config_file):
        config_path = os.path.join(STEP_FUNCTION_CONFIG_DIR, config_file)
        config_data = read_json(config_path)
        safe_print(f"Checking models from file {config_file}")

        for inference_name in config_data.get("ai_inference", {}).keys():
            model_path = inference_config.get("aws_s3_model", {}).get(inference_name, {}).get("model_directory")
            if not model_path or model_path == "null":
                continue

            model_version = config_data["ai_inference"][inference_name]["model_version"]
            if not model_version or model_version == "null":
                continue
            all_paths = [model_path] + EXTRA_MODEL_SYNC_PATHS.get(inference_name, [])

            existing_paths = []
            missing_paths = []
            for path in all_paths:
                if path_exists_in_s3(path, model_version):
                    safe_print(f"  {inference_name} - s3://{BUCKET_NAME}/{path}/{model_version}/ [{os.getenv('AWS_REGION')}] - OK")
                    existing_paths.append(path)
                else:
                    missing_paths.append(path)

            if existing_paths:
                # At least one path has the model — log missing ones but don't cross-sync.
                for path in missing_paths:
                    safe_print(f"  {inference_name} - s3://{BUCKET_NAME}/{path}/{model_version}/ - MISSING (skipped, model present in another path)")
            else:
                # No path has the model in the target bucket — sync each from its own model store path.
                for path in all_paths:
                    copy_from_model_store(inference_name, path, model_version)

    with ThreadPoolExecutor() as executor:
        list(executor.map(process_config_file, os.listdir(STEP_FUNCTION_CONFIG_DIR)))


def check_image_versions():
    ecr_client = boto3.client('ecr')
    lambda_files = ["lambdas.yaml", "lambdas_rc.yaml"]

    def process_lambda_file(lambda_file):
        safe_print(f"\n### Reading image version config from file: {lambda_file}")
        lambdas = read_yaml(lambda_file)["lambdas"]

        for lambda_entry in lambdas:
            image = lambda_entry["name"]
            tag = lambda_entry["version"]
            try:
                ecr_client.describe_images(
                    repositoryName=image,
                    imageIds=[{"imageTag": tag}]
                )
                safe_print(f"  Image: {image}:{tag} - OK")
            except ClientError as e:
                if e.response['Error']['Code'] == 'ImageNotFoundException':
                    raise RuntimeError(f"Image not found: {image}:{tag}") from e
                raise RuntimeError(f"{image}:{tag} - Unexpected ECR error: {e}") from e

    with ThreadPoolExecutor() as executor:
        list(executor.map(process_lambda_file, lambda_files))


def main():
    if len(sys.argv) != 2:
        print("Usage: python check_build_before_release.py <MODEL|IMAGE>")
        sys.exit(1)

    check_type = sys.argv[1].upper()
    try:
        if check_type == "MODEL":
            check_model_versions()
        elif check_type == "IMAGE":
            check_image_versions()
        else:
            print("Invalid argument. Use 'MODEL' or 'IMAGE'.")
            sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()