#!/usr/bin/env python3
"""Execute one selected Alfred action, with fresh validation of its target."""

import json
import os
import subprocess
import sys
import time

from common import AqaraError, Client, LOGIN_URL, Store, control_body, normalize, refresh_in_progress, selector
from keychain import KeychainError
from updater import Updater
from runtime import get_store, set_mode, validate_action


def prompt_key():
    script = ('text returned of (display dialog "Paste the aqara_api_key from agent.aqara.com/login. '
              'It will be stored in macOS Keychain." with title "Connect Aqara" '
              'default answer "" with hidden answer buttons {"Cancel", "Save"} default button "Save")')
    result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True)
    if result.returncode:
        return None
    return result.stdout.strip()


def prompt_profile_name():
    script = ('text returned of (display dialog "Enter a display name for this account or region." '
              'with title "Rename Aqara Account" default answer "" '
              'buttons {"Cancel", "Rename"} default button "Rename")')
    result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def notify_refresh_started():
    try:
        subprocess.run(["/usr/bin/osascript", "-e",
                        'display notification "Refreshing devices and scenes…" with title "Aqara"'],
                       capture_output=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        pass  # Notification permissions must not prevent a refresh.


def execute(action, store=None):
    if action.get("action") == "mode":
        directory = getattr(store, "workflow_root", store.root) if store is not None else None
        return set_mode(action.get("value"), directory)
    store = store or get_store()
    validate_action(action, store)
    if getattr(store, "is_cloud", False):
        result = store.handle_action(action)
        if result is not None:
            return result
    name = action.get("action")
    if name == "workflow_update":
        return Updater(getattr(store, "workflow_root", store.root)).install()
    if name == "workflow_autoupdate":
        if type(action.get("enabled")) is not bool:
            raise AqaraError("Choose whether to enable automatic update checks.")
        updater = Updater(getattr(store, "workflow_root", store.root))
        updater.set_enabled(action["enabled"])
        if action["enabled"]:
            updater.schedule()
        return "Automatic workflow update checks " + ("enabled." if action["enabled"] else "disabled.")
    if name == "login":
        subprocess.run(["/usr/bin/open", LOGIN_URL], check=True)
        return "Sign in, then use aq apikey to save your API key."
    if name == "apikey":
        key = prompt_key()
        if key is None:
            return "Connection cancelled."
        store.keychain.set(key)
        # A new key may belong to a different account. Do not keep old targets.
        settings = store.read("settings")
        store.write("settings", {"showstatus": settings.get("showstatus", False)})
        store.clear_catalog()
        return "API key saved for %s. Run aq update to discover its homes." % store.profile_name()
    if name == "account_add":
        label = store.validate_profile_name(action.get("name", ""))
        key = prompt_key()
        if key is None:
            return "Connection cancelled."
        client = Client(key)
        homes = normalize(client.request("homes/query"), "homes")
        if not homes:
            raise AqaraError("This credential has no visible homes. Check the account and region at aq login.")
        target = store.add_profile(label, key)
        target.activate()
        if len(homes) > 1:
            target.discover_homes(client)
            return "Connected %s with %s homes. Use aq home to choose one." % (label, len(homes))
        devices, scenes = target.refresh(client)
        return "Connected %s: %s devices and %s scenes. Use aq home to switch homes." % (label, devices, scenes)
    if name in ("account_rename", "account_rename_prompt"):
        target = store.for_profile(action.get("profile_id", store.profile_id))
        label = prompt_profile_name() if name == "account_rename_prompt" else action.get("name", "")
        if label is None:
            return "Rename cancelled."
        return "Account renamed to " + target.rename_profile(label) + "."
    if name == "account":
        target = store.for_profile(action.get("profile_id", ""))
        settings = target.read("settings")
        catalog = target.read("catalog")
        if not settings.get("home_id"):
            if not settings.get("homes"):
                target.discover_homes()
            target.activate()
            return "Switched to %s. Use aq home to choose a home." % target.profile_name()
        if catalog.get("home_id") != settings["home_id"]:
            target.refresh()
        target.activate()
        return "Switched to %s · %s." % (target.profile_name(), target.read("settings").get("home_name", "Home"))
    if name == "homes_refresh":
        updated, failed = [], []
        for profile in store.profiles():
            try:
                homes = store.for_profile(profile["id"]).discover_homes()
                updated.append((profile["name"], len(homes)))
            except (AqaraError, KeychainError, OSError) as exc:
                failed.append(profile["name"] + ": " + str(exc))
        message = "Found %s homes across %s accounts. Use aq home to choose one." % (sum(count for _, count in updated), len(updated))
        if failed:
            message += " Could not refresh " + "; ".join(failed)
        return message
    if name == "reinit":
        for profile in store.profiles():
            target = store.for_profile(profile["id"])
            target.keychain.delete()
            target.clear_catalog()
            (target.directory / "settings.json").unlink(missing_ok=True)
        (store.root / "accounts.json").unlink(missing_ok=True)
        return store.mode.title() + " mode reset."
    if name == "showstatus":
        if action.get("value") not in ("on", "off"):
            raise AqaraError("Choose showstatus on or off.")
        settings = store.read("settings")
        settings["showstatus"] = action["value"] == "on"
        store.write("settings", settings)
        return "Automatic status " + action["value"] + "."
    if name == "home":
        target = store.for_profile(action.get("profile_id", store.profile_id))
        home, catalog = target.switch_home(action.get("id"))
        target.activate()
        return "Switched to %s · %s: %s devices, %s scenes." % (target.profile_name(), home,
                len(catalog.get("devices", [])), len(catalog.get("scenes", [])))
    if name == "update":
        if (action.get("profile_id", store.profile_id) != store.profile_id
                or action.get("home_id", store.read("settings").get("home_id", "")) != store.read("settings").get("home_id", "")):
            raise AqaraError("The selected account or home changed. Select Refresh devices and scenes again.")
        if refresh_in_progress(store):
            return "Already refreshing devices and scenes. Run aq upd to see progress."
        home_id = store.read("settings").get("home_id", "")
        store.write("refresh", {"state": "running", "started": time.time(), "pid": os.getpid(), "home_id": home_id})
        notify_refresh_started()
        try:
            devices, scenes = store.refresh()
        except Exception:
            store.write("refresh", {"state": "failed", "finished": time.time(), "home_id": home_id})
            raise
        store.write("refresh", {"state": "complete", "finished": time.time(), "devices": devices, "scenes": scenes,
                                "home_id": store.read("settings").get("home_id", "")})
        return "%s · %s: refreshed %s devices and %s scenes." % (store.profile_name(), store.read("settings").get("home_name", "Home"), devices, scenes)
    if name not in ("control", "scene"):
        raise AqaraError("Unknown workflow action.")
    settings = store.read("settings")
    catalog = store.read("catalog")
    home_id = settings.get("home_id")
    if (action.get("profile_id", "default") != store.profile_id or store.active_profile_id() != store.profile_id
            or not home_id or action.get("home_id") != home_id or catalog.get("home_id") != home_id):
        raise AqaraError("The selected home changed. Search for the device or scene again.")
    targets = catalog.get("devices" if name == "control" else "scenes", [])
    target = next((target for target in targets if target["id"] == action.get("id")), None)
    if target is None:
        raise AqaraError("The selected item is no longer in the catalog. Run aq update.")
    if name == "scene":
        store.run_scene(target)
        message = "Running “" + target["name"] + "”."
    else:
        command = action.get("command", "")
        value = action.get("value", "")
        body = store.control(target, command, value)
        messages = {
            "lock": "Requesting {device} to lock.",
            "unlock": "Requesting {device} to unlock.",
            "on": "Turning {device} on.",
            "off": "Turning {device} off.",
            "dim": "Setting {device} to {value}% brightness.",
            "color": "Setting {device} to {value}.",
            "ct": "Setting {device} color temperature to {value} K.",
            "open": "Opening {device}.",
            "close": "Closing {device}.",
            "stop": "Stopping {device}.",
            "slevel": "Setting {device} to {value}% open.",
            "mode": "Setting {device} to {value} mode.",
            "temp": "Setting {device} to {value} °C.",
        }
        message = messages[command].format(device=selector(target), value=body.get("value", ""))
    # Scenes can affect several devices. Invalidate all cached readings.
    (store.directory / "status.json").unlink(missing_ok=True)
    return message


def main():
    try:
        action = json.loads(sys.argv[1])
        if not isinstance(action, dict):
            raise ValueError()
        print(execute(action))
    except (AqaraError, KeychainError, OSError, subprocess.SubprocessError) as exc:
        # stdout flows to Alfred's notification; no traceback or secret logging.
        message = "Aqara: " + str(exc)
        print(message)
        print(message, file=sys.stderr)
    except (ValueError, IndexError, TypeError, KeyError):
        message = "Aqara: Invalid action. Select an item from the workflow again."
        print(message)
        print(message, file=sys.stderr)


if __name__ == "__main__":
    main()
