#!/usr/bin/env python3
"""Create a Workspace ONE UEM device sensor from a local script file and assign it to a smart group.

Defaults to a dry-run: authenticates, resolves the Organization Group and smart group UUIDs,
checks whether a sensor with the same name already exists, and prints the payloads that would be
sent. Nothing is created or assigned unless --apply is given.

Secrets are read from the macOS keychain used by the autopkg_tools launchers
(autopkg_tools_launcher_keychain) and can be overridden with AUTOPKG_WS1_* environment variables.

Usage:
    uv run scripts/ws1_sensor_upload.py <script_file> --smart-group macOS-SW-TESTING
    uv run scripts/ws1_sensor_upload.py <script_file> --smart-group macOS-SW-TESTING --apply

API endpoints (from the tenant's own swagger at <console>/API/help):
    POST /api/mdm/devicesensors                          (version=2)  DeviceSensorRequestV2Model
    POST /api/mdm/devicesensors/{uuid}/assignment        (version=2)  DeviceSensorAssignmentRequestV1Model
    GET  /api/mdm/devicesensors/list/{og_uuid}?name=     (version=2)
    GET  /api/mdm/smartgroups/search?name=               (version=1)
    GET  /api/system/groups/search?groupid=              (version=1)  -> LocationGroup.Id.Value
    GET  /api/system/groups/{id}                         (version=1)  -> LocationGroup.Uuid
"""

import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import requests

KEYCHAIN = "autopkg_tools_launcher_keychain"
SECRET_NAMES = (
    "WS1_API_URL",
    "WS1_GROUPID",
    "WS1_OAUTH_CLIENT_ID",
    "WS1_OAUTH_CLIENT_SECRET",
    "WS1_OAUTH_TOKEN_URL",
)
QUERY_TYPES = {".zsh": "ZSH", ".sh": "BASH", ".bash": "BASH", ".py": "PYTHON"}


def get_secret(name):
    env_value = os.environ.get(f"AUTOPKG_{name}")
    if env_value:
        return env_value
    try:
        return subprocess.run(
            ["security", "find-generic-password", "-a", name, "-w", KEYCHAIN],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        sys.exit(
            f"Secret {name} not found in keychain {KEYCHAIN} nor in env AUTOPKG_{name}"
        )


def get_oauth_token(token_url, client_id, client_secret):
    r = requests.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


class Ws1Api:
    def __init__(self, api_url, token):
        self.api_url = api_url.rstrip("/")
        self.token = token

    def headers(self, version):
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": f"application/json;version={version}",
            "Content-Type": "application/json",
        }

    def get(self, path, version, **params):
        r = requests.get(
            f"{self.api_url}{path}",
            headers=self.headers(version),
            params=params,
            timeout=60,
        )
        self._check(r)
        return r.json() if r.status_code != 204 else {}

    def post(self, path, version, payload):
        r = requests.post(
            f"{self.api_url}{path}",
            headers=self.headers(version),
            json=payload,
            timeout=60,
        )
        self._check(r)
        return r.json() if r.text else {}

    @staticmethod
    def _check(r):
        if not r.ok:
            sys.exit(f"HTTP {r.status_code} for {r.request.method} {r.url}\n{r.text}")


def resolve_og_uuid(api, group_id):
    result = api.get("/api/system/groups/search", 1, groupid=group_id)
    matches = [
        g for g in result.get("LocationGroups", []) if g.get("GroupId") == group_id
    ]
    if len(matches) != 1:
        sys.exit(
            f"Expected exactly one Organization Group with GroupId {group_id}, got {len(matches)}"
        )
    # The search result omits Uuid; fetch the group by its numeric Id to get it
    og = api.get(f"/api/system/groups/{matches[0]['Id']['Value']}", 1)
    return og["Uuid"], og["Name"]


def resolve_smart_group_uuid(api, name):
    result = api.get("/api/mdm/smartgroups/search", 1, name=name)
    matches = [g for g in result.get("SmartGroups", []) if g.get("Name") == name]
    if len(matches) != 1:
        found = [g.get("Name") for g in result.get("SmartGroups", [])]
        sys.exit(
            f"Expected exactly one smart group named {name!r}, got {len(matches)}: {found}"
        )
    return matches[0]["SmartGroupUuid"]


def find_existing_sensor(api, og_uuid, name):
    result = api.get(f"/api/mdm/devicesensors/list/{og_uuid}", 2, name=name)
    return [s for s in result.get("result_set", []) if s.get("name") == name]


def build_sensor_payload(args, og_uuid, script_bytes):
    return {
        "name": args.name,
        "description": args.description,
        "platform": "APPLE_OSX",
        "query_type": args.query_type,
        "query_response_type": args.response_type,
        "organization_group_uuid": og_uuid,
        "execution_context": args.execution_context,
        "execution_architecture": "EITHER64OR32BIT",
        "script_data": base64.b64encode(script_bytes).decode("ascii"),
        "timeout": args.timeout,
    }


def build_assignment_payload(args, smart_group_uuid):
    return {
        "name": f"{args.name} - {args.smart_group}",
        "smart_group_uuids": [smart_group_uuid],
        "trigger_type": args.trigger_type,
        "event_triggers": args.event_triggers,
    }


def redacted(payload):
    shown = dict(payload)
    if "script_data" in shown:
        shown["script_data"] = f"<base64, {len(shown['script_data'])} chars>"
    return json.dumps(shown, indent=2)


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("script_file", type=Path, help="Sensor script to upload")
    p.add_argument(
        "--smart-group", required=True, help="Smart group name to assign the sensor to"
    )
    p.add_argument(
        "--name", help="Sensor name (default: script file name without extension)"
    )
    p.add_argument("--description", default="", help="Sensor description")
    p.add_argument(
        "--og-id", help="Organization Group ID (default: WS1_GROUPID from keychain)"
    )
    p.add_argument(
        "--query-type",
        choices=["ZSH", "BASH", "PYTHON"],
        help="Default derived from extension",
    )
    p.add_argument(
        "--response-type",
        default="STRING",
        choices=["STRING", "INTEGER", "BOOLEAN", "DATETIME"],
    )
    p.add_argument("--execution-context", default="SYSTEM", choices=["SYSTEM", "USER"])
    p.add_argument("--timeout", type=int, default=60, help="Script timeout in seconds")
    p.add_argument(
        "--trigger-type",
        default="SCHEDULEANDEVENT",
        choices=["SCHEDULE", "EVENT", "SCHEDULEANDEVENT"],
    )
    p.add_argument(
        "--event-triggers",
        nargs="*",
        default=["LOGIN"],
        choices=["LOGIN", "LOGOUT", "STARTUP", "USER_SWITCH", "NETWORK_CHANGE"],
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually create and assign (default is dry-run)",
    )
    return p.parse_args()


def main():
    args = parse_args()
    if not args.script_file.is_file():
        sys.exit(f"Script file not found: {args.script_file}")
    args.name = args.name or args.script_file.stem
    args.query_type = args.query_type or QUERY_TYPES.get(args.script_file.suffix)
    if not args.query_type:
        sys.exit(
            f"Cannot derive query type from extension {args.script_file.suffix!r}; use --query-type"
        )

    secrets = {name: get_secret(name) for name in SECRET_NAMES}
    og_id = args.og_id or secrets["WS1_GROUPID"]
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] WS1 API: {secrets['WS1_API_URL']}")

    token = get_oauth_token(
        secrets["WS1_OAUTH_TOKEN_URL"],
        secrets["WS1_OAUTH_CLIENT_ID"],
        secrets["WS1_OAUTH_CLIENT_SECRET"],
    )
    api = Ws1Api(secrets["WS1_API_URL"], token)
    print("[ok] OAuth token obtained")

    og_uuid, og_name = resolve_og_uuid(api, og_id)
    print(f"[ok] Organization Group {og_id} ({og_name}) -> {og_uuid}")

    sg_uuid = resolve_smart_group_uuid(api, args.smart_group)
    print(f"[ok] Smart group {args.smart_group!r} -> {sg_uuid}")

    existing = find_existing_sensor(api, og_uuid, args.name)
    if existing:
        sys.exit(
            f"Sensor named {args.name!r} already exists (uuid {existing[0]['uuid']}, "
            f"{existing[0].get('assignment_count')} assignments). Delete or rename first."
        )
    print(f"[ok] No existing sensor named {args.name!r}")

    sensor_payload = build_sensor_payload(args, og_uuid, args.script_file.read_bytes())
    assignment_payload = build_assignment_payload(args, sg_uuid)
    print(f"\nPOST /api/mdm/devicesensors (version=2):\n{redacted(sensor_payload)}")
    print(
        f"\nPOST /api/mdm/devicesensors/<uuid>/assignment (version=2):\n{redacted(assignment_payload)}"
    )

    if not args.apply:
        print("\n[DRY-RUN] Nothing sent. Re-run with --apply to create and assign.")
        return

    created = api.post("/api/mdm/devicesensors", 2, sensor_payload)
    sensor_uuid = created.get("uuid")
    if not sensor_uuid:
        sys.exit(f"Sensor create returned no uuid: {created}")
    print(f"\n[ok] Sensor created: uuid {sensor_uuid}")

    assigned = api.post(
        f"/api/mdm/devicesensors/{sensor_uuid}/assignment", 2, assignment_payload
    )
    print(f"[ok] Assignment created: {assigned or 'no body'}")


if __name__ == "__main__":
    main()
