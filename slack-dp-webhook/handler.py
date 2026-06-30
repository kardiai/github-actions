import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request

SLACK_SIGNING_SECRET   = os.environ["SLACK_SIGNING_SECRET"]
SLACK_BOT_TOKEN        = os.environ["SLACK_BOT_TOKEN"]
GITHUB_TOKEN           = os.environ["GITHUB_TOKEN"]
GITHUB_REPO            = os.environ.get("GITHUB_REPO", "kardiai/release-management")
ALLOWED_SLACK_USER_IDS = set(os.environ["ALLOWED_SLACK_USER_IDS"].split(","))
PROMOTE_WORKFLOW_FILE  = os.environ.get("PROMOTE_WORKFLOW_FILE", "create-dp-promote-pr.yaml")
UPGRADE_WORKFLOW_FILE  = os.environ.get("UPGRADE_WORKFLOW_FILE", "create-dp-upgrade-pr.yaml")


# ---------------------------------------------------------------------------
# Slack & GitHub helpers
# ---------------------------------------------------------------------------

def verify_signature(headers: dict, body: str) -> bool:
    ts = headers.get("x-slack-request-timestamp", "")
    if not ts or abs(time.time() - int(ts)) > 300:
        return False
    sig_base = f"v0:{ts}:{body}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        sig_base.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, headers.get("x-slack-signature", ""))


def slack_api(method: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {SLACK_BOT_TOKEN}")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def github_dispatch(workflow_file: str, ref: str, inputs: dict) -> None:
    owner, repo = GITHUB_REPO.split("/")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_file}/dispatches",
        data=json.dumps({"ref": ref, "inputs": inputs}).encode(),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req):
        pass


def ok(body: str | dict = "") -> dict:
    if isinstance(body, dict):
        body = json.dumps(body)
    return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": body}


# ---------------------------------------------------------------------------
# Slack modal definitions
# ---------------------------------------------------------------------------

def _static_select(block_id: str, label: str, options: list[tuple[str, str]]) -> dict:
    return {
        "type": "input",
        "block_id": block_id,
        "label": {"type": "plain_text", "text": label},
        "element": {
            "type": "static_select",
            "action_id": "value",
            "options": [
                {"text": {"type": "plain_text", "text": text}, "value": value}
                for text, value in options
            ],
        },
    }


def build_promote_modal(channel_id: str) -> dict:
    return {
        "type": "modal",
        "callback_id": "dp_promote_modal",
        "private_metadata": channel_id,
        "title": {"type": "plain_text", "text": "Promote DP Package"},
        "submit": {"type": "plain_text", "text": "Create PR"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            _static_select("source_branch", "Source branch (TRAINING)", [
                ("TEST-TRAINING", "TEST-TRAINING"),
            ]),
            _static_select("source_slot", "Source slot", [
                ("primary", "primary"),
                ("secondary", "secondary"),
            ]),
            _static_select("target_env", "Target environment", [
                ("test-secondary", "test-secondary"),
                ("prod-secondary", "prod-secondary"),
            ]),
        ],
    }


def build_upgrade_modal(channel_id: str) -> dict:
    return {
        "type": "modal",
        "callback_id": "dp_upgrade_modal",
        "private_metadata": channel_id,
        "title": {"type": "plain_text", "text": "Upgrade DP Package"},
        "submit": {"type": "plain_text", "text": "Create PR"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            _static_select("target_env", "Prostředí (secondary → primary)", [
                ("TEST  (secondary → primary)", "test"),
                ("PROD  (secondary → primary)", "prod"),
            ]),
        ],
    }


# ---------------------------------------------------------------------------
# Command handler  (/dp-promote, /dp-upgrade)
# ---------------------------------------------------------------------------

def handle_command(params: dict) -> dict:
    command    = params.get("command",    [""])[0]
    user_id    = params.get("user_id",   [""])[0]
    trigger_id = params.get("trigger_id",[""])[0]
    channel_id = params.get("channel_id",[""])[0]

    if user_id not in ALLOWED_SLACK_USER_IDS:
        return ok({"response_type": "ephemeral", "text": "⛔ Nemáš oprávnění pro tuto operaci."})

    if command == "/dp-promote":
        modal = build_promote_modal(channel_id)
    elif command == "/dp-upgrade":
        modal = build_upgrade_modal(channel_id)
    else:
        return ok()

    slack_api("views.open", {"trigger_id": trigger_id, "view": modal})
    return ok()


# ---------------------------------------------------------------------------
# Modal submission handler
# ---------------------------------------------------------------------------

def _block_value(view: dict, block_id: str) -> str:
    return view["state"]["values"][block_id]["value"]["selected_option"]["value"]


def _post_initial_slack_message(channel_id: str, text: str) -> str:
    resp = slack_api("chat.postMessage", {
        "channel": channel_id,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "⏳ Vytvářím PR..."}]},
        ],
    })
    return resp.get("ts", "")


def handle_promote_submission(view: dict, user: dict) -> dict:
    source_branch = _block_value(view, "source_branch")
    source_slot   = _block_value(view, "source_slot")
    target_env    = _block_value(view, "target_env")
    target_branch = "TEST" if target_env == "test-secondary" else "PROD"
    channel_id    = view.get("private_metadata", "")
    user_name     = user["username"]

    slack_ts = _post_initial_slack_message(
        channel_id,
        f"🚀 *Promote request*\n`{source_branch}/{source_slot}` → `{target_env}`\nRequested by: @{user_name}",
    )

    github_dispatch(PROMOTE_WORKFLOW_FILE, "TEST-TRAINING", {
        "source_branch":   source_branch,
        "source_slot":     source_slot,
        "target_env":      target_env,
        "target_branch":   target_branch,
        "requested_by":    user_name,
        "slack_channel":   channel_id,
        "slack_thread_ts": slack_ts,
    })

    return ok({"response_action": "clear"})


def handle_upgrade_submission(view: dict, user: dict) -> dict:
    target_env    = _block_value(view, "target_env")
    target_branch = target_env.upper()
    channel_id    = view.get("private_metadata", "")
    user_name     = user["username"]

    slack_ts = _post_initial_slack_message(
        channel_id,
        f"⬆️ *Upgrade request*\n`{target_env}/secondary` → `{target_env}/primary`\nRequested by: @{user_name}",
    )

    github_dispatch(UPGRADE_WORKFLOW_FILE, "TEST-TRAINING", {
        "target_env":      target_env,
        "target_branch":   target_branch,
        "requested_by":    user_name,
        "slack_channel":   channel_id,
        "slack_thread_ts": slack_ts,
    })

    return ok({"response_action": "clear"})


def handle_interaction(payload: dict) -> dict:
    if payload.get("type") != "view_submission":
        return ok()

    view     = payload["view"]
    user     = payload["user"]
    callback = view["callback_id"]

    if callback == "dp_promote_modal":
        return handle_promote_submission(view, user)
    if callback == "dp_upgrade_modal":
        return handle_upgrade_submission(view, user)

    return ok()


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
    body_raw = event.get("body", "")
    if event.get("isBase64Encoded"):
        body_raw = base64.b64decode(body_raw).decode()

    if not verify_signature(headers, body_raw):
        return {"statusCode": 401, "body": "Unauthorized"}

    parsed = urllib.parse.parse_qs(body_raw)

    if "payload" in parsed:
        return handle_interaction(json.loads(parsed["payload"][0]))

    return handle_command(parsed)
