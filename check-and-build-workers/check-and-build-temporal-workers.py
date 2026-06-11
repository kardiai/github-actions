import yaml
import requests
import os
import time
import sys
import argparse
import datetime
import base64
import re


def main():
    parser = argparse.ArgumentParser(description='Check and build Temporal workers.')
    parser.add_argument('--env', required=True, choices=['primary', 'secondary'],
                        help='Environment to check (primary or secondary)')
    parser.add_argument('--versions-file', default='versions.yaml',
                        help='Path to the versions YAML file')
    parser.add_argument('--force', action='store_true',
                        help='Apply all tags to k8s-deployment values, even if unchanged')
    args = parser.parse_args()

    token = os.environ.get('GITHUB_TOKEN')
    owner = os.environ.get('OWNER')
    docker_user = os.environ.get('DOCKER_USERNAME')
    docker_pass = os.environ.get('DOCKER_ACCESS_TOKEN')
    slack_token = os.environ.get('SLACK_TOKEN')
    slack_channel = os.environ.get('SLACK_CHANNEL')
    development_environment = os.environ.get('DEVELOPMENT_ENVIRONMENT')

    if not token or not owner:
        print("ERROR: GITHUB_TOKEN and OWNER environment variables must be set.")
        sys.exit(1)

    if not docker_user or not docker_pass:
        print("ERROR: DOCKER_USERNAME and DOCKER_ACCESS_TOKEN environment variables must be set.")
        sys.exit(1)

    # # Headers for GitHub API communication
    raw_headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github.v3.raw'}
    api_headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github.v3+json'}

    # Authenticate with Docker Hub
    print(f"[{args.env.upper()}] Authenticating with Docker Hub...")
    login_resp = requests.post(
        'https://hub.docker.com/v2/users/login',
        json={'username': docker_user, 'password': docker_pass}
    )
    if login_resp.status_code != 200:
        print(f"ERROR: Docker Hub authentication failed: {login_resp.status_code} - {login_resp.text}")
        sys.exit(1)

    docker_token = login_resp.json().get('token')
    docker_headers = {'Authorization': f'Bearer {docker_token}'}

    # 1. Load local versions yaml file
    print(f"[{args.env.upper()}] Loading local file: {args.versions_file}")
    with open(args.versions_file, 'r') as f:
        versions = yaml.safe_load(f)

    # 2. Download and load remote values file based on environment
    values_url = f'https://api.github.com/repos/{owner}/k8s-deployment/contents/helm/values/kardiai-processing/eu-central-1/{development_environment}/values-{args.env}.yaml'
    print(f"[{args.env.upper()}] Downloading Helm values: {values_url}")

    r_values = requests.get(values_url, headers=raw_headers)
    r_values.raise_for_status()
    values_env = yaml.safe_load(r_values.text)

    # Extract all tags from helm values for comparison
    deployed_tags = []
    for lambda_data in values_env.get('lambdas', {}).values():
        if 'image' in lambda_data and 'tag' in lambda_data['image']:
            deployed_tags.append(lambda_data['image']['tag'])

    print(f"[{args.env.upper()}] Found {len(deployed_tags)} deployed tags in Helm values.")
    # Configure groups (versions.yaml section -> docker repo, git repo)

    kardiai_repos = ('temporal-kardi-worker', 'aws-lambdas')
    ai_repos = ('temporal-ai-worker', 'ai-noise-model')

    to_build = []
    ready_to_deploy = []

    # Process temporal-workflow-version first (no building, just update values)
    workflow_versions = versions.get('temporal-workflow-version', [])
    if not isinstance(workflow_versions, list):
        workflow_versions = []

    for item in workflow_versions:
        name = item.get('name')
        expected_tag = item.get('version')

        if not name or not expected_tag:
            continue

        # Pokusíme se najít stávající tag přímo ze stažených helm values
        old_tag = None
        lambda_data = values_env.get('lambdas', {}).get(name)
        if lambda_data and 'image' in lambda_data and 'tag' in lambda_data['image']:
            old_tag = lambda_data['image']['tag']

        # Zkontrolujeme, zda nastala změna, a případně zařadíme rovnou k nasazení
        if old_tag and (old_tag != expected_tag or args.force):
            if old_tag == expected_tag:
                print(f"[{args.env.upper()}] Force mode: re-applying workflow {name}: {expected_tag}")
            else:
                print(f"[{args.env.upper()}] Change detected for workflow {name}: {old_tag} -> {expected_tag}")

            # Check Docker Hub to ensure the image is actually ready
            dh_resp = requests.get(
                f"https://hub.docker.com/v2/repositories/kardiai/temporal-workflow-worker/tags/{expected_tag}",
                headers=docker_headers
            )
            if dh_resp.status_code == 200:
                print(f" -> Docker image kardiai/temporal-workflow-worker:{expected_tag} is ready.")
                ready_to_deploy.append({
                    'name': name,
                    'old_tag': old_tag,
                    'new_tag': expected_tag
                })
            else:
                print(f" -> WARNING: Docker image kardiai/temporal-workflow-worker:{expected_tag} NOT FOUND on Docker Hub. Skipping update.")

    # 3. Process changes based on version logic (flat list)
    all_items = versions.get('temporal-worker-versions', [])
    if not isinstance(all_items, list):
        all_items = []

    for item in all_items:
        if 'temporal-worker' not in item:
            continue

        name = item['name']
        version = item['version']
        temporal_worker = item['temporal-worker']

        # Determine repos based on the version prefix
        # 0.8.x belongs to AI group, others (0.0.1, 0.9.x) belong to Kardiai group
        if version.startswith('0.8.'):
            docker_repo, git_repo = ai_repos
        else:
            docker_repo, git_repo = kardiai_repos

        # Format tag: name_version_temporal-worker_value
        ## needs to go short here - 63 chars max for docker tag
        expected_tag = f"{name}_{version}_tp-{temporal_worker}"

        # Find the old tag and image name for this specific worker to enable replacement
        lambda_data = values_env.get('lambdas', {}).get(name, {})
        old_tag = lambda_data.get('image', {}).get('tag')
        old_image_name = lambda_data.get('image', {}).get('name', '')

        # Check if a change occurred (expected tag not in container repo)
        if old_tag and (expected_tag != old_tag or args.force):
            if expected_tag == old_tag:
                print(f"[{args.env.upper()}] Force mode: re-applying {name}: {expected_tag}")
            else:
                print(f"[{args.env.upper()}] Change detected for {name}: Expected tag {expected_tag}")

            ready_item = {
                'name': name,
                'old_tag': old_tag,
                'new_tag': expected_tag
            }

            # check if image name changed as well (temporal-kardi-worker vs temporal-ai-worker)
            expected_image_name = docker_repo
            if old_image_name and old_image_name != expected_image_name:
                print(f" -> Image name change detected: {old_image_name} -> {expected_image_name}")
                ready_item['old_image_name'] = old_image_name
                ready_item['new_image_name'] = expected_image_name

            ready_to_deploy.append(ready_item)

            # Docker Hub check
            dh_resp = requests.get(
                f"https://hub.docker.com/v2/repositories/kardiai/{docker_repo}/tags/{expected_tag}",
                headers=docker_headers
            )
            if dh_resp.status_code == 200:
                print(f" -> Docker image kardiai/{docker_repo}:{expected_tag} already exists. Skipping build.")
            else:
                # Git tag check
                worker_tag = f"{name}@{version}"
                temporal_tag = f"temporal-worker@{temporal_worker}"

                wt_resp = requests.get(
                    f"https://api.github.com/repos/{owner}/{git_repo}/git/refs/tags/{worker_tag}",
                    headers=api_headers)
                tt_resp = requests.get(
                    f"https://api.github.com/repos/{owner}/{git_repo}/git/refs/tags/{temporal_tag}",
                    headers=api_headers)

                if wt_resp.status_code == 200 and tt_resp.status_code == 200:
                    print(
                        f" -> Found git tags ({worker_tag}, {temporal_tag}) in repo {git_repo}. Adding to build queue.")
                    to_build.append({
                        'git_repo': git_repo,
                        'docker_repo': docker_repo,
                        'expected_tag': expected_tag,
                        'worker_tag': worker_tag,
                        'temporal_tag': temporal_tag
                    })
                else:
                    print(
                        f" -> Missing git tags ({worker_tag}, {temporal_tag}) in repo {git_repo}. Skipping build.")

    # Trigger build with dispatch
    trigger_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%SZ')

    for item in to_build:
        dispatch_url = f"https://api.github.com/repos/{owner}/{item['git_repo']}/actions/workflows/build-temporal-worker.yaml/dispatches"
        payload = {
            "ref": "develop",
            "inputs": {
                "worker_tag": item['worker_tag'],
                "temporal_tag": item['temporal_tag']
            }
        }
        res = requests.post(dispatch_url, headers=api_headers, json=payload)
        if res.status_code == 204:
            print(f"[{args.env.upper()}] Successfully triggered workflow in {item['git_repo']} for tag {item['expected_tag']}")
        else:
            print(f"[{args.env.upper()}] Error triggering workflow: {res.status_code} - {res.text}")

    # 7. Wait for image build and check GitHub Actions for failures
    if to_build:
        print(f"\n[{args.env.upper()}] Waiting for builds to finish (checking GitHub Actions and Docker Hub)...")
        max_retries = 30  # 60 * 30s = 30 minutes timeout
        pending_items = to_build.copy()

        for i in range(max_retries):
            # Check Docker Hub for completed images
            still_pending = []
            for item in pending_items:
                dh_resp = requests.get(
                    f"https://hub.docker.com/v2/repositories/kardiai/{item['docker_repo']}/tags/{item['expected_tag']}",
                    headers=docker_headers
                )
                if dh_resp.status_code == 200:
                    print(f" -> Image {item['expected_tag']} successfully pushed to Docker Hub!")
                else:
                    still_pending.append(item)

            pending_items = still_pending
            if not pending_items:
                print(f"[{args.env.upper()}] All images present in Docker Hub!")
                break

            # Check GitHub Actions for early failures
            pending_repos = set(item['git_repo'] for item in pending_items)
            for git_repo in pending_repos:
                runs_url = f"https://api.github.com/repos/{owner}/{git_repo}/actions/workflows/build-temporal-worker.yaml/runs"
                runs_resp = requests.get(
                    runs_url,
                    headers=api_headers,
                    params={'event': 'workflow_dispatch', 'created': f'>={trigger_time}'}
                )
                if runs_resp.status_code == 200:
                    runs_data = runs_resp.json().get('workflow_runs', [])
                    for run in runs_data:
                        if run['status'] == 'completed' and run['conclusion'] in ['failure', 'cancelled', 'startup_failure']:
                            print(f"\n[{args.env.upper()}] ERROR: Detected failed workflow run in {git_repo}! Failing fast.")
                            print(f" -> Failed run URL: {run.get('html_url')}")
                            sys.exit(1)

            print(f"[{args.env.upper()}] Still waiting for {len(pending_items)} image(s)... (sleeping 60s)")
            print(f"{[item['expected_tag'] for item in pending_items]}")
            time.sleep(60)
        else:
            print(f"[{args.env.upper()}] ERROR: Timeout while waiting for images!")
            sys.exit(1)

    # 8. Create PR to k8s-deployment
    pr_url = None
    if ready_to_deploy:
        print(f"\n[{args.env.upper()}] Preparing Pull Request for k8s-deployment...")
        repo_name = "k8s-deployment"
        file_path = f"helm/values/kardiai-processing/eu-central-1/{development_environment}/values-{args.env}.yaml"
        commit_message = f"Update Temporal workers for {args.env}"

        # Apply replacements to the raw yaml text (preserves comments and structure)
        new_yaml_content = r_values.text
        for item in ready_to_deploy:
            pattern = rf"^([ \t]*{re.escape(item['name'])}:(?:\n[ \t]+.*)*?\n[ \t]+tag:[ \t]*){re.escape(item['old_tag'])}"
            new_yaml_content = re.sub(pattern, rf"\g<1>{item['new_tag']}", new_yaml_content, flags=re.MULTILINE)

        # Apply image name replacements specifically under the worker's key
        for item in ready_to_deploy:
            if 'old_image_name' in item and 'new_image_name' in item:
                # Regex bezpečně najde blok dané lambdy a vymění v něm name
                pattern = rf"^([ \t]*{item['name']}:(?:\n[ \t]+.*)*?\n[ \t]+name:[ \t]*){re.escape(item['old_image_name'])}"
                new_yaml_content = re.sub(pattern, rf"\g<1>{item['new_image_name']}", new_yaml_content, flags=re.MULTILINE)

        # Get default branch SHA (assuming 'main' or 'master')
        repo_info_resp = requests.get(f"https://api.github.com/repos/{owner}/{repo_name}", headers=api_headers)
        default_branch = repo_info_resp.json().get('default_branch', 'main')
        ref_resp = requests.get(f"https://api.github.com/repos/{owner}/{repo_name}/git/ref/heads/{default_branch}", headers=api_headers)
        base_sha = ref_resp.json()['object']['sha']

        if development_environment in ['test', 'training']:
            # Commit directly to default branch
            file_sha_resp = requests.get(f"https://api.github.com/repos/{owner}/{repo_name}/contents/{file_path}", headers=api_headers, params={'ref': default_branch})
            file_sha = file_sha_resp.json()['sha']
            encoded_content = base64.b64encode(new_yaml_content.encode('utf-8')).decode('utf-8')
            update_resp = requests.put(
                f"https://api.github.com/repos/{owner}/{repo_name}/contents/{file_path}",
                headers=api_headers,
                json={
                    "message": commit_message,
                    "content": encoded_content,
                    "sha": file_sha,
                    "branch": default_branch
                }
            )
            update_resp.raise_for_status()
            print(f"[{args.env.upper()}] File updated directly on {default_branch} branch.")
        else:
            # Create PR
            branch_name = f"update-workers-{args.env}-{int(time.time())}"
            ref_resp = requests.get(f"https://api.github.com/repos/{owner}/{repo_name}/git/ref/heads/{default_branch}", headers=api_headers)
            base_sha = ref_resp.json()['object']['sha']

            # Create new branch
            requests.post(
                f"https://api.github.com/repos/{owner}/{repo_name}/git/refs",
                headers=api_headers,
                json={"ref": f"refs/heads/{branch_name}", "sha": base_sha}
            ).raise_for_status()

            # Update file
            file_sha_resp = requests.get(f"https://api.github.com/repos/{owner}/{repo_name}/contents/{file_path}", headers=api_headers, params={'ref': branch_name})
            file_sha = file_sha_resp.json()['sha']
            encoded_content = base64.b64encode(new_yaml_content.encode('utf-8')).decode('utf-8')
            requests.put(
                f"https://api.github.com/repos/{owner}/{repo_name}/contents/{file_path}",
                headers=api_headers,
                json={
                    "message": commit_message,
                    "content": encoded_content,
                    "sha": file_sha,
                    "branch": branch_name
                }
            ).raise_for_status()

            # Create PR
            pr_body = "Automated PR to update Temporal worker versions.\n\nChanges:\n"
            for item in ready_to_deploy:
                image_name_change = f" *(image name changed to `{item['new_image_name']}`)*" if 'new_image_name' in item else ""
                pr_body += f"- `{item['name']}`: `{item['old_tag']}` -> `{item['new_tag']}`{image_name_change}\n"

            pr_resp = requests.post(
                f"https://api.github.com/repos/{owner}/{repo_name}/pulls",
                headers=api_headers,
                json={
                    "title": commit_message,
                    "head": branch_name,
                    "base": default_branch,
                    "body": pr_body
                }
            )
            pr_resp.raise_for_status()
            pr_url = pr_resp.json()['html_url']
            print(f"[{args.env.upper()}] Pull Request successfully created: {pr_url}")

    # Notify Slack
    if ready_to_deploy and slack_token and slack_channel:
        actual_changes = [item for item in ready_to_deploy if item['old_tag'] != item['new_tag']]
        if actual_changes:
            print(f"\n[{args.env.upper()}] Sending notification to Slack...")

            changes_text = "\n".join([f"• *{item['name']}*: `{item['old_tag']}` ➔ `{item['new_tag']}`" for item in actual_changes])

            if pr_url:
                changes_text += f"\n\n🔗 *Review and Merge PR here:* <{pr_url}|View Pull Request>"

            # URL pro Slack Web API
            api_url = "https://slack.com/api/chat.postMessage"

            # Hlavičky s ověřením pomocí tokenu
            headers = {
                "Authorization": f"Bearer {slack_token}",
                "Content-Type": "application/json"
            }

            # Sestavení zprávy s určením kanálu a vzhledu
            slack_msg = {
                "channel": slack_channel,
                "username": "Temporal Deployer",
                "icon_url": "https://docs.temporal.io/img/favicon.png",
                "attachments": [
                    {
                        "color": "good",
                        "title": f"🚀 New Temporal Workers ready for {args.env.upper()} environment!",
                        "text": changes_text,
                        "footer": "send by KardiAI Github Actions"
                    }
                ]
            }

            # Odeslání přes API
            response = requests.post(api_url, headers=headers, json=slack_msg)

            # Volitelná kontrola odpovědi ze Slacku pro lepší debugging
            if response.status_code == 200 and response.json().get("ok"):
                print(f"[{args.env.upper()}] Slack notification sent successfully to {slack_channel}.")
            else:
                print(f"[{args.env.upper()}] FAILED to send Slack notification. Response: {response.text}")
        else:
            print(f"\n[{args.env.upper()}] No actual changes detected. Skipping Slack notification.")

    elif ready_to_deploy and not (slack_token and slack_channel):
        print(f"\n[{args.env.upper()}] WARNING: SLACK_TOKEN or SLACK_CHANNEL is missing. Skipping Slack notification.")

    print(f"[{args.env.upper()}] All done.")


if __name__ == '__main__':
    main()
