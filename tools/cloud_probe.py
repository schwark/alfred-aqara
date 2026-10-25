#!/usr/bin/env python3
"""Read-only cloud inventory for locally connected Aqara Mobile profiles.

No login secrets are accepted on the command line. Private evidence is written
only beneath ~/.cache/alfred-aqara; stdout contains counts and safe error codes.
This tool is deliberately excluded from Alfred release packages.
"""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import AqaraError
from cloud_capabilities import definition_endpoints, panel_endpoints
from mobile_backend import MobileStore
from mobile_transport import MobileAPIError


PRIVATE_ROOT = Path.home() / ".cache/alfred-aqara"
SECRET_FIELDS = {"token", "accesstoken", "refreshtoken", "password", "appkey", "authorization", "cookie", "setcookie"}


def private_evidence(value):
    if isinstance(value, dict):
        return {key: private_evidence(child) for key, child in value.items()
                if str(key).casefold().replace("_", "").replace("-", "") not in SECRET_FIELDS}
    if isinstance(value, list):
        return [private_evidence(child) for child in value]
    return value


def save_evidence(destination, value):
    destination = Path(destination).expanduser().resolve()
    if PRIVATE_ROOT.resolve() not in destination.parents:
        raise AqaraError("Cloud evidence must stay under the private Aqara cache directory.")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".cloud-probe-", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(private_evidence(value), stream, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def error_code(exc):
    return {"error": "query_failed", "code": exc.code} if isinstance(exc, MobileAPIError) else {"error": "query_failed"}


def inspect_device(client, row, home_id=None, bridges=False, definitions=None):
    did = str(row["did"])
    result = {"catalog_entry": row}
    for name, call in (("panels", client.panels), ("matter", client.matter_details), ("details", client.details)):
        try:
            result[name] = call(did)
        except AqaraError as exc:
            result[name] = error_code(exc)
    endpoints = panel_endpoints(result.get("panels"), did)
    if definitions:
        endpoints += definition_endpoints(definitions, str(row.get("model") or ""))
    paths = sorted({path for endpoint in endpoints for path in endpoint["paths"]})
    result["trait_pages"] = []
    for start in range(0, len(paths), 300):
        try:
            result["trait_pages"].append(client.traits(did, paths[start:start + 300]))
        except AqaraError as exc:
            result["trait_pages"].append(error_code(exc))
    if bridges and home_id:
        try:
            result["bridge_gateways"] = client.bridge_gateways(did, home_id)
        except AqaraError as exc:
            result["bridge_gateways"] = error_code(exc)
    return result


def inspect_profile(store, include_devices=False, bridges=False):
    if not store.secrets().get("token"):
        return {"status": "sign_in_required"}, {"status": "sign_in_required"}
    client = store.mobile_client()
    homes = client.catalog("homes")
    result = {"profile_id": store.profile_id, "homes": []}
    try:
        result["specification"] = client.specification()
    except AqaraError as exc:
        result["specification"] = error_code(exc)
    definitions = None
    if include_devices:
        try:
            definitions = client.definitions()
            result["endpoint_definitions"] = definitions
        except AqaraError as exc:
            result["endpoint_definitions"] = error_code(exc)
    device_count = 0
    for home in homes:
        identifier = str(home.get("homeId") or home["positionId"])
        devices = client.catalog("devices", identifier)
        device_count += len(devices)
        inspected = [inspect_device(client, row, identifier, bridges, definitions) for row in devices] if include_devices else devices
        entry = {"catalog_entry": home, "devices": inspected}
        if bridges:
            # Only query gateways identified by returned device relationships
            # or bridge metadata; do not guess hub IDs or modify bridge setup.
            gateways = {str(row["parentDeviceId"]) for row in devices if row.get("parentDeviceId")}
            for device in inspected:
                rows = device.get("bridge_gateways", [])
                if isinstance(rows, list):
                    gateways.update(str(row["deviceId"]) for row in rows if isinstance(row, dict) and row.get("deviceId"))
            entry["gateways"] = []
            for gateway in sorted(gateways):
                record = {"device_id": gateway}
                for name, call in (("details", lambda: client.gateway_details(gateway)),
                                   ("bridge_devices", lambda: client.bridge_devices(identifier, gateway)),
                                   ("matter", lambda: client.matter_details(gateway))):
                    try:
                        record[name] = call()
                    except AqaraError as exc:
                        record[name] = error_code(exc)
                entry["gateways"].append(record)
        result["homes"].append(entry)
    return result, {"status": "queried", "homes": len(homes), "devices": device_count}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--devices", action="store_true", help="Also read every device's endpoints, traits, and extended metadata")
    parser.add_argument("--bridges", action="store_true", help="Also read gateway and Matter bridge metadata; implies --devices")
    parser.add_argument("--output", type=Path, default=PRIVATE_ROOT / "cloud-evidence.json")
    args = parser.parse_args()
    store = MobileStore()
    evidence = {"captured_at": time.time(), "profiles": []}
    summaries = []
    for profile in store.profiles():
        try:
            data, summary = inspect_profile(store.for_profile(profile["id"]), args.devices or args.bridges, args.bridges)
        except AqaraError as exc:
            data = summary = error_code(exc)
        evidence["profiles"].append(data)
        summaries.append(summary)
    save_evidence(args.output, evidence)
    print(json.dumps({"profiles": summaries, "device_writes": 0}))
    return 0 if all(s.get("status") == "queried" for s in summaries) else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AqaraError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
