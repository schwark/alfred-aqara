#!/usr/bin/env python3
"""Alfred Script Filter. Browsing never changes device state."""

import ast
import json
import re
import sys

from common import AqaraError, Store, control_body, controls, selector, table_rows
from keychain import KeychainError
from updater import Updater


def item(title, subtitle="", action=None, autocomplete=None, uid=None):
    value = {"title": title, "subtitle": subtitle, "valid": action is not None}
    if action is not None:
        value["arg"] = json.dumps(action, ensure_ascii=False)
    if autocomplete is not None:
        value["autocomplete"] = autocomplete
    if uid is not None:
        value["uid"] = uid
    return value


def scoped(store, action, **kwargs):
    return dict(action=action, profile_id=store.profile_id,
                home_id=store.read("settings").get("home_id", ""), **kwargs)


def scope_label(store):
    return store.profile_name() + " · " + store.read("settings").get("home_name", "Choose a home")


def home_items(query, store):
    term = query.partition(" ")[2].strip().casefold()
    if term in ("update", "refresh"):
        return [item("Refresh homes from all accounts", "Discover homes for every saved account or region",
                     {"action": "homes_refresh"})]
    result = []
    for profile in store.profiles():
        target = store.for_profile(profile["id"])
        settings = target.read("settings")
        for home in settings.get("homes", []):
            if not all(token in (home["name"] + " " + profile["name"]).casefold() for token in term.split()):
                continue
            selected = profile["id"] == store.profile_id and home["id"] == settings.get("home_id")
            subtitle = profile["name"] + (" · Current home" if selected else " · Switch to this home")
            result.append((not selected, item(home["name"], subtitle,
                {"action": "home", "profile_id": profile["id"], "id": home["id"]},
                uid="home:" + profile["id"] + ":" + home["id"])))
    result.sort(key=lambda entry: (entry[0], entry[1]["title"].casefold(), entry[1]["subtitle"].casefold()))
    result = [entry[1] for entry in result]
    if not result:
        result.append(item("No matching homes" if term else "No homes discovered yet", "Refresh homes or add another account below"))
    result.extend([
        item("Refresh homes from all accounts", "Discover new homes without changing your selected home", {"action": "homes_refresh"}),
        item("Add an account or region…", "Save a separate Aqara API key for another login or region", autocomplete="account add "),
    ])
    return result


def account_items(query, store):
    term = query.partition(" ")[2].strip()
    operation, _, name = term.partition(" ")
    if operation.casefold() == "rename" and not name.strip():
        return [item("Rename " + profile["name"], "Choose a new display name for this account or region",
                     {"action": "account_rename_prompt", "profile_id": profile["id"]}, uid="rename:" + profile["id"])
                for profile in store.profiles()]
    if operation.casefold() in ("add", "rename"):
        renaming = operation.casefold() == "rename"
        try:
            name = store.validate_profile_name(name, renaming=renaming)
            action = {"action": "account_rename" if renaming else "account_add", "name": name,
                      "profile_id": store.profile_id}
            return [item(("Rename current account to " if renaming else "Connect account or region: ") + name,
                         "Keep its saved credential and homes" if renaming else "Paste this account's API key into the secure dialog", action)]
        except AqaraError as exc:
            return [item("Name this account or region", str(exc), autocomplete="account " + operation.casefold() + " ")]
    result = []
    for profile in store.profiles():
        if term.casefold() not in profile["name"].casefold():
            continue
        target = store.for_profile(profile["id"])
        settings = target.read("settings")
        subtitle = ("Current account" if profile["id"] == store.profile_id else "Switch account")
        subtitle += " · %s homes" % len(settings.get("homes", []))
        if settings.get("home_name"):
            subtitle += " · " + settings["home_name"]
        result.append(item(profile["name"], subtitle, {"action": "account", "profile_id": profile["id"]}, uid="account:" + profile["id"]))
    if not term:
        result.extend([
            item("Add an account or region…", "Sign in with aq login, then save its key under a name", autocomplete="account add "),
            item("Rename an account…", "Choose any saved account or region", autocomplete="account rename "),
        ])
    return result or [item("No matching accounts", "Use aq account add <name> to connect another account or region")]


def status_items(store, device):
    data = store.status(device)
    result = []
    hidden = {"device_id", "device_ids", "endpoint_id", "position_id", "home_id", "requestid",
              "request_id", "code", "message", "msg", "success", "endpoint_name",
              "device_name", "device_type", "position_name", "remote_control_switch"}
    labels = {"lock_state": "Lock", "door_state": "Door", "online_offline": "Connection",
              "on_off": "Power", "color_temperature": "Color temperature"}
    values = {"doorclosed": "Closed", "dooropen": "Open", "dooropened": "Open",
              "locked": "Locked", "unlocked": "Unlocked", "online": "Online", "offline": "Offline",
              "on": "On", "off": "Off"}

    def label_text(path):
        def label(part):
            return labels.get(part, re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", part).replace("_", " ").capitalize())
        return " · ".join(label(part) for part in path.split(" · ")) if path else "Status"

    def value_text(value):
        if isinstance(value, bool):
            return "Yes" if value else "No"
        return values.get(str(value).casefold(), str(value))

    def walk(value, path=""):
        if isinstance(value, str) and len(value) > 16384 and value.lstrip().startswith(("{", "[")):
            result.append(item(label_text(path) + ": Unavailable", selector(device) + " · Aqara returned an oversized status value"))
            return
        if isinstance(value, str) and len(value) <= 16384 and value.lstrip().startswith(("{", "[")):
            parsed = None
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                try:
                    parsed = ast.literal_eval(value)
                except (ValueError, SyntaxError, RecursionError, MemoryError):
                    pass
            if isinstance(parsed, (dict, list)):
                walk(parsed, "" if path == "status" else path)
                return
            result.append(item(label_text(path) + ": Unavailable", selector(device) + " · Aqara returned an unreadable status value"))
            return
        if isinstance(value, dict):
            for key, child in value.items():
                key = str(key)
                if key.casefold() not in hidden:
                    walk(child, path if key in ("data", "result", "results", "status") else (path + " · " + key if path else key))
        elif isinstance(value, list):
            value = table_rows(value)
            for index, child in enumerate(value):
                walk(child, path if len(value) == 1 else path + " " + str(index + 1))
        elif value is not None:
            result.append(item("%s: %s" % (label_text(path), value_text(value)), selector(device)))

    walk(data)
    summary = " · ".join(field["title"] for field in result) if result else "No status values returned"
    row = item(selector(device), summary, autocomplete=selector(device) + " ")
    row["text"] = {"copy": summary, "largetype": summary}
    return [row]


def device_items(store, device, remainder):
    label = selector(device)
    context = label + " · " + scope_label(store)
    titles = {"on": "Turn on", "off": "Turn off", "open": "Open", "close": "Close", "stop": "Stop",
              "dim": "Set brightness", "color": "Set color", "ct": "Set color temperature",
              "slevel": "Set position", "mode": "Set mode", "temp": "Set temperature"}
    units = {"dim": "%", "slevel": "%", "ct": " K", "temp": " °C"}
    commands = controls(device)
    if remainder.strip().casefold() == "status":
        return status_items(store, device)
    word, _, value = remainder.strip().partition(" ")
    word = word.casefold()
    lock_note = item(label, "Lock/unlock unavailable through Aqara Agent; another connection is required")
    if "doorlock" in device.get("type", "").casefold() and word in ("lock", "unlock"):
        return [lock_note]
    result = []
    for name, (_attribute, _action, hint) in commands.items():
        if word and not name.startswith(word):
            continue
        complete = label + " " + name
        if name == "status":
            if not word and store.read("settings").get("showstatus"):
                continue
            result.append(item("Show status", context, autocomplete=complete))
            continue
        needs_value = name in ("dim", "ct", "color", "slevel", "mode", "temp")
        action = scoped(store, "control", id=device["id"], command=name, value="")
        subtitle = context
        title = titles[name]
        if needs_value:
            complete += " "
            subtitle = context + " · Enter " + hint
            action = None
            suggestions = {"mode": ("auto", "heat", "cool", "dry", "fan"),
                           "color": ("red", "green", "blue", "yellow", "orange", "cyan", "magenta", "purple", "pink", "white")}
            if word == name and name in suggestions:
                options = [option for option in suggestions[name] if option.startswith(value.strip().casefold())]
                if options:
                    for option in options:
                        result.append(item(title + " to " + option, context,
                                           scoped(store, "control", id=device["id"], command=name, value=option),
                                           label + " " + name + " " + option))
                    continue
            if word == name and value.strip():
                complete += value.strip()
                try:
                    control_body(device, name, value.strip())
                    action = scoped(store, "control", id=device["id"], command=name, value=value.strip())
                    title += " to " + value.strip() + units.get(name, "")
                    subtitle = context
                except AqaraError as exc:
                    subtitle = context + " · " + str(exc)
        elif value.strip():
            action = None
            subtitle = context + " · This command does not accept a value"
        result.append(item(title, subtitle, action, complete))
    if "doorlock" in device.get("type", "").casefold() and not word:
        result.append(lock_note)
    if not result and not word and store.read("settings").get("showstatus"):
        return []
    return result or [item(label, "No matching command — select the device again to see its commands", autocomplete=label + " ")]


def matching_devices(query, devices, names_only=False):
    tokens = query.casefold().split()
    def search_text(device):
        fields = [selector(device), device.get("device_name", "")]
        if not names_only:
            fields += [device.get("room", ""), device.get("type", "")]
        return " ".join(fields).casefold()
    return [device for device in devices if all(token in search_text(device) for token in tokens)]


def selected_device_items(store, device, remainder):
    result = device_items(store, device, remainder)
    if not remainder and store.read("settings").get("showstatus"):
        try:
            result = status_items(store, device) + result
        except (AqaraError, KeychainError) as exc:
            result.insert(0, item(selector(device), "Status unavailable: " + str(exc), autocomplete=selector(device) + " "))
    return result


def device_choices(devices, remainder=""):
    return [item(selector(device), "Choose this device" + (" · " + remainder if remainder else ""),
                 autocomplete=selector(device) + " " + remainder, uid="device:" + device["id"])
            for device in devices]


def build_items(query, store=None, updater=None):
    store = store or Store()
    updater = updater or Updater(store.root)
    term = query.strip().casefold()
    commands = [
        ("workflow:update", "Check for an Aqara workflow update", "Download the latest release and open Alfred's upgrade dialog", {"action": "workflow_update"}),
        ("workflow:autoupdate", "Enable automatic update checks", "Check GitHub once a day while using the workflow", {"action": "workflow_autoupdate", "enabled": True}),
        ("workflow:noautoupdate", "Disable automatic update checks", "You can still check manually with aq workflow:update", {"action": "workflow_autoupdate", "enabled": False}),
    ]
    if term == "upgrade":
        term = "workflow:update"
    if term.startswith("workflow:"):
        return [item(title, subtitle, action, keyword, "update:" + keyword)
                for keyword, title, subtitle, action in commands if keyword.startswith(term)] or [
                    item("No matching workflow command", "Use aq workflow:update to check for updates", autocomplete="workflow:update")]
    result = _build_items(query, store)
    if not term:
        result.append(item("Workflow updates", "Check for updates or change automatic checks", autocomplete="workflow:"))
    status = updater.available()
    if status:
        result.insert(0, item("Aqara " + status["version"] + " is available", "Install the update · Confirm the upgrade in Alfred",
                              {"action": "workflow_update"}, "workflow:update", "workflow-update"))
    return result


def _build_items(query, store):
    query = query.strip()
    settings = store.read("settings")
    systems = [
        ("login", "Sign in to Aqara", "Open the Aqara sign-in page", {"action": "login"}),
        ("apikey", "Replace key for " + store.profile_name(), "Paste the Aqara API key into a secure dialog", {"action": "apikey"}),
        ("update", "Refresh devices and scenes", "Also discovers homes on your account", {"action": "update"}),
        ("home", "Choose Aqara home", settings.get("home_name", "No home selected"), None),
        ("account", "Manage accounts and regions", store.profile_name(), None),
        ("showstatus", "Show status automatically", "Choose on or off; off keeps browsing fast", None),
        ("reinit", "Reset Aqara workflow", "Remove all saved accounts, keys, home selections, and catalogs", {"action": "reinit"}),
    ]
    first = query.partition(" ")[0].casefold()
    if first in ("home", "homes"):
        return home_items(query, store)
    if first in ("account", "accounts"):
        return account_items(query, store)
    if query.casefold().startswith("showstatus"):
        term = query[len("showstatus"):].strip().casefold()
        return [item("Automatic status " + value, "Fetch live status when you select a device" if value == "on" else "Fetch status only with the status command",
                     {"action": "showstatus", "value": value})
                for value in ("on", "off") if value.startswith(term)]
    for keyword, title, subtitle, action in systems:
        if query.casefold() == keyword and action is not None:
            return [item(title, subtitle, action)]
    catalog = store.read("catalog")
    if catalog.get("home_id") != settings.get("home_id"):
        catalog = {}
    devices = catalog.get("devices", [])
    scenes = catalog.get("scenes", [])
    device_query = query[7:] if query.casefold().startswith("device ") else query
    name_candidates = matching_devices(device_query, devices, names_only=True) if device_query else []
    candidates = name_candidates or (matching_devices(device_query, devices) if device_query else [])
    # Longest matching name wins, so a device named "Desk Light on" is selectable.
    matches = []
    for device in devices:
        for label in {selector(device), device["name"]}:
            if device_query.casefold() == label.casefold() or device_query.casefold().startswith(label.casefold() + " "):
                matches.append((len(label), device, device_query[len(label):].lstrip()))
    if matches:
        longest = max(match[0] for match in matches)
        selected = {match[1]["id"]: match for match in matches if match[0] == longest}
        if len(selected) == 1:
            _, device, remainder = next(iter(selected.values()))
            command = remainder.partition(" ")[0].casefold()
            known_command = not command or any(name.startswith(command) for name in controls(device)) or command in ("lock", "unlock")
            if known_command or not candidates:
                return selected_device_items(store, device, remainder)
        else:
            return device_choices([match[1] for match in selected.values()], next(iter(selected.values()))[2])
    if device_query and not name_candidates:
        words = device_query.split()
        known_commands = {name for device in devices for name in controls(device)} | {"lock", "unlock"}
        for index in range(len(words) - 1, 0, -1):
            if not any(name.startswith(words[index].casefold()) for name in known_commands):
                continue
            partial = " ".join(words[:index])
            remainder = " ".join(words[index:])
            matches = matching_devices(partial, devices)
            if len(matches) == 1:
                return selected_device_items(store, matches[0], remainder)
            if matches:
                return device_choices(matches, remainder)
    if len(candidates) == 1:
        return selected_device_items(store, candidates[0], "")
    scene_only = query.casefold().startswith("scene ") or query.casefold() == "scene"
    search = query[5:].strip() if scene_only else device_query
    tokens = search.casefold().split()
    result = []
    if not scene_only:
        for device in devices:
            haystack = " ".join([selector(device), device.get("device_name", ""), device.get("room", ""), device.get("type", "")]).casefold()
            if all(token in haystack for token in tokens):
                result.append(item(selector(device), " · ".join(filter(None, [device.get("room"), device.get("type"), "Choose a command"])),
                                   autocomplete=selector(device) + " ", uid="device:" + device["id"]))
    for scene in scenes:
        if all(token in (scene["name"] + " " + scene.get("room", "")).casefold() for token in tokens):
            result.append(item(scene["name"], "Run scene · " + scope_label(store) + (" · " + scene["room"] if scene.get("room") else ""),
                               scoped(store, "scene", id=scene["id"]), uid="scene:" + scene["id"]))
    if not scene_only:
        for keyword, title, subtitle, action in systems:
            if not query or keyword.startswith(query.casefold()):
                if not query and keyword == "home":
                    continue
                result.append(item(title, subtitle, action, keyword + (" " if action is None else "")))
    if not query:
        result.insert(0, item(scope_label(store), "Switch homes across your saved accounts and regions", autocomplete="home "))
    if not devices and not scenes and not query:
        if settings.get("homes"):
            result.insert(0, item("Choose a home", "Select which home's devices and scenes to browse", autocomplete="home "))
        else:
            result.insert(0, item("Set up Aqara", "Use aq login, aq apikey, then aq update"))
    return result or [item("No matching devices or scenes", "Try a room or device name, or aq update to refresh")]


def main():
    try:
        store = Store()
        updater = Updater(store.root)
        updater.schedule()
        result = build_items(sys.argv[1] if len(sys.argv) > 1 else "", store, updater)
    except (AqaraError, KeychainError, OSError) as exc:
        result = [item("Aqara unavailable", str(exc))]
        print("Aqara: " + str(exc), file=sys.stderr)
    print(json.dumps({"items": result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
