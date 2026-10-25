"""Persist Agent/Cloud mode independently of either mode's account selection."""

import json
import os
from pathlib import Path
import tempfile
import uuid

from common import AqaraError, BUNDLE_ID, Store
from keychain import Keychain


def workflow_root(directory=None):
    return Path(directory or os.environ.get("alfred_workflow_data") or
                Path.home() / "Library/Application Support/Alfred/Workflow Data" / BUNDLE_ID)


def mode_state(directory=None):
    value = Store._read_path(workflow_root(directory) / "mode.json")
    mode = value.get("mode", "agent")
    return (mode if mode in ("agent", "cloud") else "agent", value.get("revision", ""))


def migrate_cloud(directory, legacy=None):
    """Copy the earlier parallel installation once, without changing its data."""
    from cloud_backend import BUNDLE
    root = workflow_root(directory)
    destination = root / "cloud"
    legacy = Path(legacy or Path.home() / "Library/Application Support/Alfred/Workflow Data/com.schwark.aqara.mobile")
    if destination.exists() or not legacy.is_dir():
        return False
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    accounts = Store._read_path(legacy / "accounts.json")
    profiles = accounts.get("profiles", [{"id": "default", "name": "Current account"}])
    identifiers = [p.get("id") for p in profiles if isinstance(p, dict)]
    if len(identifiers) != len(profiles) or any(
            not isinstance(i, str) or not i or Path(i).name != i or i in (".", "..") for i in identifiers):
        raise AqaraError("The earlier Cloud account list is invalid. Agent mode is unchanged.")
    created = []
    try:
        with tempfile.TemporaryDirectory(prefix=".cloud-migration-", dir=str(root)) as temporary:
            staging = Path(temporary) / "cloud"
            staging.mkdir(mode=0o700)
            if (legacy / "accounts.json").exists():
                Store._write_path(staging / "accounts.json", accounts)
            for identifier in identifiers:
                relative = Path() if identifier == "default" else Path("profiles") / identifier
                for name in ("settings", "catalog", "catalogs", "definitions"):
                    source = legacy / relative / (name + ".json")
                    if source.is_file():
                        value = json.loads(source.read_text())
                        _rename_cached_labels(value)
                        Store._write_path(staging / relative / source.name, value)
            for account in ["app-identity"] + ["connection:" + i for i in identifiers]:
                target = Keychain(service=BUNDLE, account=account)
                existing = target.get()
                value = Keychain(service="com.schwark.aqara.mobile", account=account).get()
                if existing and value and existing != value:
                    raise AqaraError("Cloud credentials already exist without their account data. Migration kept both copies unchanged.")
                if not existing and value:
                    created.append(target)
                    target.set(value)
            os.replace(staging, destination)
    except Exception:
        for target in reversed(created):
            target.delete()
        raise
    return True


def _rename_cached_labels(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("name", "label", "device_name", "home_name") and isinstance(child, str):
                value[key] = child.removesuffix(" (Mobile)") + (" (Cloud)" if child.endswith(" (Mobile)") else "")
            elif isinstance(child, (dict, list)):
                _rename_cached_labels(child)
    elif isinstance(value, list):
        for child in value:
            _rename_cached_labels(child)


def set_mode(mode, directory=None):
    if mode not in ("agent", "cloud"):
        raise AqaraError("Choose aq mode agent or aq mode cloud.")
    root = workflow_root(directory)
    if mode == "cloud":
        migrate_cloud(root)
    if mode_state(root)[0] != mode:
        Store._write_path(root / "mode.json", {"mode": mode, "revision": uuid.uuid4().hex})
    return "Using %s mode. Run aq to browse, or aq login to connect." % mode.title()


def get_store(directory=None):
    root = workflow_root(directory)
    mode, revision = mode_state(root)
    if mode == "cloud":
        migrate_cloud(root)
        from cloud_backend import CloudStore
        store = CloudStore(root / "cloud")
    else:
        store = Store(root)
    store.workflow_root = root
    store.mode_revision = revision

    def guard():
        if mode_state(root) != (mode, revision):
            raise AqaraError("The mode changed. Select the device or scene again.")
    store.mode_guard = guard
    return store


def validate_action(action, store):
    if action.get("mode", store.mode) != store.mode or (
            "mode_revision" in action and action["mode_revision"] != getattr(store, "mode_revision", "")):
        raise AqaraError("The mode changed. Select the device or scene again.")
    store.ensure_current_mode()
