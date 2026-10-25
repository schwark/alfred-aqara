"""Cloud mode, with explicit Agent fallback for feature parity.

Device writes use common, capability-advertised traits and Matter lock commands.
Unknown native controls remain read-only. No model-name dispatch,
automatic write retry, legacy pairing-key extraction, or guessed resource writes.
"""

import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

from common import AqaraError, Client, Store, control_body, controls, normalize
from cloud_capabilities import (compile_traits, definition_endpoints, objects, panel_endpoints, trait_rows,
                                trait_status, validate_control)
from keychain import Keychain
from cloud_transport import ALLOWED, AppIdentity, CloudAPIError, CloudClient
from cloud_identity import public_identity


BUNDLE = "com.schwark.aqara.cloud"
# Keep legacy serialized IDs/metadata keys so migrated catalogs and home links
# retain their exact targets. All user-facing labels call this Cloud mode.
SCENES = "/app/position/scene/query"
MATTER_WRITE = "/matter/write"
SCENE_RUN = "/ifttt/scene/run"
TRAIT_WRITE = "/app/qlink/trait/write"


def identity_key():
    return Keychain(service=BUNDLE, account="app-identity")


def public_identity_key():
    return Keychain(service=BUNDLE, account="public-app-identity")


def save_identity(reference):
    identity = (AppIdentity.from_json(reference) if Path(reference).suffix.casefold() == ".json"
                else AppIdentity.from_reference(reference))
    identity_key().set(json.dumps(identity.payload()))


def load_identity():
    override = identity_key().get()
    if not override:
        return public_identity(public_identity_key())
    try:
        return AppIdentity.from_payload(json.loads(override))
    except (ValueError, TypeError, CloudAPIError):
        raise AqaraError("The advanced Cloud signing override is invalid. Use aq identity to replace it.") from None


def page_rows(value, kind):
    """Known app response containers only; reject malformed/incomplete catalogs."""
    if isinstance(value, list) and all(isinstance(row, dict) for row in value):
        return value, None
    if isinstance(value, dict):
        for key in {"homes": ("homes", "positions", "list"), "devices": ("devices", "data", "list"),
                    "scenes": ("scenes", "list")}[kind]:
            if isinstance(value.get(key), list):
                result, _ = page_rows(value[key], kind)
                count = value.get("count", value.get("total"))
                return result, count if type(count) is int and count >= 0 else None
    raise AqaraError("The Cloud API returned an unexpected %s catalog. Previous data was kept." % kind)


class BackendClient(CloudClient):
    routes = ALLOWED | {("GET", SCENES), ("POST", MATTER_WRITE), ("POST", SCENE_RUN), ("POST", TRAIT_WRITE)}

    def catalog(self, kind, home_id=""):
        result, seen = [], set()
        for start in range(0, 30000, 300):
            value = (self.homes(start) if kind == "homes" else self.devices(home_id, start)
                     if kind == "devices" else self._call("GET", SCENES,
                         {"positionId": home_id, "startIndex": start, "size": 300}))
            page, count = page_rows(value, kind)
            fields = {"homes": ("homeId", "positionId"), "devices": ("did",), "scenes": ("sceneId",)}[kind]
            ids = [str(next((row[key] for key in fields if isinstance(row.get(key), (str, int))
                             and not isinstance(row[key], bool) and row[key]), "")) for row in page]
            if any(not identifier or identifier in seen for identifier in ids) or len(ids) != len(set(ids)):
                raise AqaraError("The Cloud API repeated a page or omitted an identifier. Previous data was kept.")
            result.extend(page)
            seen.update(ids)
            if count is not None and len(result) > count:
                raise AqaraError("The Cloud API returned inconsistent page counts. Previous data was kept.")
            if count is not None and len(result) == count:
                return result
            if len(page) < 300:
                if count is not None and len(result) < count:
                    raise AqaraError("The Cloud API returned an incomplete catalog. Previous data was kept.")
                return result
        raise AqaraError("The Cloud catalog exceeds the supported page limit. Previous data was kept.")

    def write_lock(self, did, path, password=""):
        # Exactly the shared Android request builder, with an explicit single write.
        if not re.fullmatch(r"\d+\.148\.(35010|35011)\.0", path):
            raise AqaraError("The selected device did not advertise a supported lock command.")
        return self._call("POST", MATTER_WRITE, {"did": did,
            "pwd": hashlib.md5(password.encode("utf-8")).hexdigest() if password else "",
            "data": {path: ""}, "type": 0})

    def write_trait(self, did, spec, value=""):
        # Restrict this writer to supported semantic controls. In particular it
        # cannot write DoorLock traits or an arbitrary path from an action JSON.
        suffix = {"power": "132.32920", "brightness": "133.32923", "mired": "134.32927"}.get(spec.get("kind"))
        path = spec.get("path", "")
        if not suffix or not re.fullmatch(r"\d+\." + re.escape(suffix), path):
            raise AqaraError("This native control is not implemented.")
        wire_value = validate_control(spec, value)
        return self._call("POST", TRAIT_WRITE, {"deviceId": did, "traits": [{"path": path, "value": wire_value}]})

    def model_endpoints(self, model):
        cache_path = getattr(self, "definition_cache", None)
        cached = Store._read_path(cache_path) if cache_path else {}
        value = getattr(self, "_definitions", None)
        if value is None:
            if time.time() - cached.get("updated", 0) < 86400 and isinstance(cached.get("data"), dict):
                value = cached["data"]
            else:
                value = self.definitions()
                if cache_path:
                    Store._write_path(cache_path, {"updated": time.time(), "data": value})
            self._definitions = value
        return definition_endpoints(value, model)

    def endpoint_capabilities(self, did, model=""):
        """Read both shared schemas; one unavailable schema does not erase the other."""
        panels, matter, failures = [], [], 0
        try:
            panels = panel_endpoints(self.panels(did), did)
        except CloudAPIError:
            failures += 1
        try:
            matter = capabilities(self.matter_details(did), did)
        except CloudAPIError:
            failures += 1
        by_ep = {c["endpoint"]: dict(c) for c in matter}
        seeds = []
        if model:
            try:
                seeds = self.model_endpoints(model)
            except CloudAPIError:
                failures += 1
        for seed in seeds:
            entry = by_ep.setdefault(seed["endpoint"], {"endpoint": seed["endpoint"], "commands": {}, "readings": {}})
            entry.update(seed)
        for panel in panels:
            entry = by_ep.setdefault(panel["endpoint"], {"endpoint": panel["endpoint"], "commands": {}, "readings": {}})
            paths = set(entry.get("paths", [])) | set(panel["paths"])
            entry.update(panel)
            entry["paths"] = sorted(paths)
        for entry in by_ep.values():
            # The authenticated trait can coexist with standard cloud commands.
            # Its presence does not imply those advertised commands are invalid.
            if any(path.endswith(".148.40000") for path in entry.get("paths", [])):
                entry["has_authenticated_route"] = True
        paths = sorted({p for entry in by_ep.values() for p in entry.get("paths", [])})
        traits = {}
        for start in range(0, len(paths), 300):
            page = paths[start:start + 300]
            try:
                traits.update(trait_rows(self.traits(did, page), did, page))
            except CloudAPIError:
                failures += 1
        for entry in by_ep.values():
            combined = {p: dict(entry.get("seed_traits", {}).get(p, {}), **row) for p, row in traits.items()}
            lock_state = traits.get(entry["endpoint"] + ".148.32970", {})
            if str(lock_state.get("value")) in ("0", "1", "2"):
                entry["commands"].update(entry.get("definition_commands", {}))
            entry["cloud_controls"], entry["trait_readings"] = compile_traits(entry, combined)
            commands = entry["cloud_controls"]
            entry["trait_values"] = ({commands["on"]["path"]: {commands["on"]["value"]: "On", commands["off"]["value"]: "Off"}}
                                     if "on" in commands and "off" in commands else {})
        return list(by_ep.values()), failures


def capabilities(payload, did):
    """Bind commands to this device's endpoints, never the global command list."""
    result = []
    if not isinstance(payload, dict) or not isinstance(payload.get("devices"), list):
        return result
    matches = [d for d in payload["devices"] if isinstance(d, dict) and str(d.get("did", "")) == did]
    if len(matches) != 1:
        return result
    for device in matches:
        for endpoint in objects(device.get("eps")):
            if not isinstance(endpoint, dict) or not re.fullmatch(r"\d+", str(endpoint.get("id", ""))):
                continue
            ep = str(endpoint["id"])
            commands, readings = {}, {}
            for function in objects(endpoint.get("funcs")):
                if str(function.get("id")) != "148":
                    continue
                for cmd in objects(function.get("cmds")):
                    name = {"35010": "lock", "35011": "unlock"}.get(str(cmd.get("id")))
                    if name:
                        commands[name] = ep + ".148." + str(cmd["id"]) + ".0"
                for trait in objects(function.get("traits")):
                    name = {"32970": "Lock", "32971": "Door"}.get(str(trait.get("id")))
                    if name:
                        readings[ep + ".148." + str(trait["id"]) + ".1"] = name
            if commands or readings:
                result.append({"endpoint": ep, "commands": commands, "readings": readings})
    return result


def readings(payload, paths=None):
    """Reduce resource responses to known, human-readable status fields."""
    paths = paths or {}
    named = {"lock_state": "Lock", "batt_0_remain_percentage": "Battery",
             "device_offline_status": "Connection"}
    enums = {"Lock": {"0": "Not fully locked", "1": "Locked", "2": "Unlocked"},
             "Door": {"0": "Open", "1": "Closed", "2": "Jammed", "3": "Forced open",
                      "4": "Error", "5": "Ajar"}}
    result = {}

    def walk(value):
        if isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, dict):
            resource = str(value.get("resourceId", value.get("attr", value.get("resource", ""))))
            label = paths.get(resource) or named.get(resource)
            if label and "value" in value and isinstance(value["value"], (str, int, float, bool)):
                raw = str(value["value"])
                if len(raw) > 128 or raw.lstrip().startswith(("{", "[")):
                    return
                text = enums.get(label, {}).get(raw, raw) if resource in paths else raw
                if label == "Lock" and resource not in paths:
                    text = {"locked": "Locked", "unlocked": "Unlocked"}.get(raw.casefold(), "Reported state " + raw)
                if label == "Battery":
                    text += "%"
                if label == "Connection":
                    text = {"0": "Online", "1": "Offline", "online": "Online", "offline": "Offline"}.get(raw, "Unknown")
                result[label] = text
            for key, child in value.items():
                if key in named and isinstance(child, (str, int, float)):
                    walk({"attr": key, "value": child})
                elif isinstance(child, (dict, list)):
                    walk(child)
    walk(payload)
    return result or {"Status": "No readable status values returned"}


def dialog(message, title="Aqara Cloud", hidden=False):
    script = ('text returned of (display dialog ' + json.dumps(message) + ' with title ' + json.dumps(title)
              + ' default answer "" ' + ('with hidden answer ' if hidden else '')
              + 'buttons {"Cancel", "Continue"} default button "Continue")')
    result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True)
    return result.stdout.rstrip("\n") if result.returncode == 0 else None


class CloudStore(Store):
    is_cloud = True
    mode = "cloud"

    def __init__(self, directory=None, keychain=None, profile_id=None, keychain_factory=None):
        from runtime import workflow_root
        directory = directory or workflow_root() / "cloud"
        factory = keychain_factory or (lambda identifier: Keychain(service=BUNDLE, account="connection:" + identifier))
        super().__init__(directory, keychain, profile_id, factory)

    @staticmethod
    def cloud_catalog(catalog):
        """Project older mixed caches onto the Cloud inventory without a refresh."""
        if not catalog:
            return catalog
        agent_names = {d["name"] for d in catalog.get("devices", []) if d.get("backend") != "mobile"}
        devices = []
        for device in catalog.get("devices", []):
            if device.get("backend") != "mobile":
                continue
            device = dict(device)
            name = device["name"]
            if name.endswith(" (Cloud)") and name.removesuffix(" (Cloud)") in agent_names:
                device["name"] = device["device_name"] = name.removesuffix(" (Cloud)")
            devices.append(device)
        labels = normalize([{"endpoint_id": d["id"], "device_name": d["name"],
                             "position_name": d.get("room", ""), "device_type": d.get("type", "")}
                            for d in devices], "devices")
        by_id = {d["id"]: d for d in devices}
        return dict(catalog, devices=[dict(by_id[d["id"]], label=d["label"]) for d in labels])

    def read(self, name):
        value = super().read(name)
        if name == "catalog":
            return self.cloud_catalog(value)
        if name == "catalogs":
            return {identifier: self.cloud_catalog(catalog) for identifier, catalog in value.items()}
        return value

    def secrets(self):
        try:
            value = json.loads(self.keychain.get() or "{}")
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
        raise AqaraError("Reconnect this Cloud account with aq login.")

    def cloud_client(self):
        value = self.secrets()
        if not value.get("token") or not value.get("user_id"):
            raise AqaraError("Connect the Cloud account with aq login first.")
        client = BackendClient(load_identity(), value.get("area", ""), value.get("district", ""))
        client.token, client.user_id = value["token"], value["user_id"]
        settings = self.read("settings")
        home = next((h for h in settings.get("homes", []) if h["id"] == settings.get("home_id")), {})
        client.position_id = settings.get("home_links", {}).get(home.get("id"), home.get("mobile_id", ""))
        client.definition_cache = self.directory / "definitions.json"
        return client

    def client(self):
        value = self.secrets()
        if not value.get("agent_key"):
            raise AqaraError("This action needs the Agent fallback. Use aq apikey or aq import.")
        home = self.read("settings").get("home_id", "")
        return Client(value["agent_key"], home)

    def discover_homes(self, client=None):
        value, homes = self.secrets(), []
        settings = self.read("settings")
        links = settings.get("home_links", {})
        if value.get("agent_key"):
            for home in normalize((client or self.client()).request("homes/query"), "homes"):
                homes.append(dict(home, agent=True))
        if value.get("token"):
            for row in self.cloud_client().catalog("homes"):
                identifier = str(row.get("homeId") or row["positionId"])
                matches = [home for home in homes if home["id"] == identifier or links.get(home["id"]) == identifier]
                if len(matches) > 1:
                    raise AqaraError("A Cloud home is linked more than once. Remove the duplicate home link.")
                existing = matches[0] if matches else None
                if existing:
                    existing["mobile_id"] = identifier
                else:
                    name = str(row.get("homeName") or row.get("positionName") or "Home")
                    if any(home["name"].casefold() == name.casefold() for home in homes):
                        name += " (Cloud)"
                    homes.append({"id": "mobile:" + identifier, "mobile_id": identifier, "name": name})
        if not homes:
            raise AqaraError("No homes found. Connect the Cloud account with aq login or import Agent profiles.")
        if not any(home["id"] == settings.get("home_id") for home in homes):
            settings.pop("home_id", None)
            settings.pop("home_name", None)
            for name in ("catalog", "status"):
                (self.directory / (name + ".json")).unlink(missing_ok=True)
        if len(homes) == 1:
            settings.update(home_id=homes[0]["id"], home_name=homes[0]["name"])
        settings["homes"] = homes
        self.write("settings", settings)
        self.write("catalogs", {key: val for key, val in self.read("catalogs").items()
                                 if key in {home["id"] for home in homes}})
        return homes

    def link_home(self, agent_id, mobile_id=None):
        settings = self.read("settings")
        if not any(h["id"] == agent_id and h.get("agent") for h in settings.get("homes", [])):
            raise AqaraError("Choose an Agent home before linking its Cloud counterpart.")
        if mobile_id is not None:
            ids = {str(h.get("homeId") or h.get("positionId")) for h in self.cloud_client().catalog("homes")}
            if mobile_id not in ids:
                raise AqaraError("The Cloud home is no longer available in this account.")
        links = dict(settings.get("home_links", {}))
        if mobile_id is None:
            links.pop(agent_id, None)
        else:
            if any(value == mobile_id and key != agent_id for key, value in links.items()):
                raise AqaraError("This Cloud home is already linked to another Agent home.")
            links[agent_id] = mobile_id
        settings["home_links"] = links
        self.write("settings", settings)
        self.clear_catalog()
        return "Home link saved. Run aq update." if mobile_id else "Home link removed. Run aq update."

    def refresh(self, client=None):
        self.discover_homes(client)
        settings = self.read("settings")
        home = next((h for h in settings["homes"] if h["id"] == settings.get("home_id")), None)
        if not home:
            raise AqaraError("Choose a home with aq home, then run aq update.")
        devices, agent_devices, scenes = [], [], []
        if home.get("agent"):
            agent = client or self.client()
            agent.home_id = home["id"]
            agent_devices = normalize(agent.request("home/devices/query"), "devices")
            scenes = normalize(agent.request("home/scenes/query"), "scenes")
        native_failures = 0
        if home.get("mobile_id"):
            mobile = self.cloud_client()
            for row in mobile.catalog("devices", home["mobile_id"]):
                did = str(row["did"])
                # Query common capabilities for every device. No model whitelist.
                try:
                    caps, failures = mobile.endpoint_capabilities(did, str(row.get("model") or ""))
                    native_failures += bool(failures)
                except CloudAPIError:
                    caps = []
                    native_failures += 1
                endpoints = caps or [{"endpoint": "", "commands": {}, "readings": {}}]
                for cap in endpoints:
                    name = str(cap.get("name") or row.get("deviceName") or row.get("originalName") or "Device")
                    if len(endpoints) > 1 and not cap.get("name"):
                        name += " · Endpoint " + cap["endpoint"]
                    device = {"id": "mobile:" + did + ("#" + cap["endpoint"] if cap["endpoint"] else ""),
                              "did": did, "model": str(row.get("model") or ""), "pwd_state": row.get("pwdState"),
                              "endpoint": cap["endpoint"], "name": name, "device_name": name,
                              "room": str(row.get("positionName") or ""), "kind": "device", "backend": "mobile",
                              "type": cap.get("type") or ("DoorLock" if cap["commands"] or cap["readings"] or any(".148." in p for p in cap.get("paths", [])) else str(row.get("model") or "Device")),
                              "mobile_commands": cap["commands"], "mobile_readings": cap["readings"],
                              "cloud_controls": cap.get("cloud_controls", {}), "trait_readings": cap.get("trait_readings", {}),
                              "trait_values": cap.get("trait_values", {}),
                              "lock_note": "This endpoint needs an authenticated lock-control route; it has not been validated yet"}
                    # Merge only an exact identifier; never associate a write by name.
                    subject_key = cap.get("subject_key")
                    unique_subject = subject_key and sum(c.get("subject_key") == subject_key for c in endpoints) == 1
                    matches = [d for d in agent_devices if
                               ((unique_subject and d["id"] == subject_key) or (len(endpoints) == 1 and d["id"] == did))]
                    existing = matches[0] if len(matches) == 1 else None
                    if existing and caps:
                        device["agent_fallback"] = {"id": existing["id"], "type": existing["type"]}
                    devices.append(device)
            native_scenes = mobile.catalog("scenes", home["mobile_id"])
            for row in native_scenes:
                identifier = str(row["sceneId"])
                if not any(scene["id"] == identifier for scene in scenes):
                    name = str(row.get("sceneName") or row.get("name") or "Scene")
                    if any(s["name"].casefold() == name.casefold() for s in scenes):
                        name += " (Cloud)"
                    scenes.append({"id": "mobile:" + identifier, "scene_id": identifier, "backend": "mobile",
                                   "name": name,
                                   "room": str(row.get("positionName") or ""), "kind": "scene"})
        else:
            native_scenes = []
        # Reuse the existing duplicate-name disambiguation without losing backend metadata.
        labels = normalize([{"endpoint_id": d["id"], "device_name": d["name"],
                             "position_name": d["room"], "device_type": d["type"]} for d in devices], "devices")
        by_id = {d["id"]: d for d in devices}
        devices = [dict(by_id[d["id"]], label=d["label"]) for d in labels]
        catalog = {"home_id": home["id"], "updated": time.time(), "devices": devices, "scenes": scenes,
                   "native_scene_count": len(native_scenes), "native_capability_failures": native_failures}
        self.write("catalog", catalog)
        cached = self.read("catalogs")
        cached[home["id"]] = catalog
        self.write("catalogs", cached)
        (self.directory / "status.json").unlink(missing_ok=True)
        return len(devices), len(scenes)

    def status(self, device, client=None):
        if device.get("backend") != "mobile":
            return super().status(device, client)
        cache = self.read("status")
        key = self.read("settings").get("home_id", "") + ":" + device["id"]
        entry = cache.get(key, {})
        if time.time() - entry.get("updated", 0) < 5:
            return entry["data"]
        mobile = client or self.cloud_client()
        paths = device.get("mobile_readings", {})
        trait_paths = device.get("trait_readings", {})
        if trait_paths:
            data = trait_status(mobile.traits(device["did"], trait_paths), device["did"], trait_paths, device.get("trait_values"))
        elif not paths and device.get("agent_fallback"):
            return super().status(device["agent_fallback"], client=None)
        else:
            data = readings(mobile.matter_status(device["did"], paths) if paths else mobile.status(device["did"]), paths)
        cache[key] = {"updated": time.time(), "data": data}
        self.write("status", cache)
        return data

    def run_scene(self, scene):
        if scene.get("backend") == "mobile":
            mobile = self.cloud_client()
            self.ensure_current_mode()
            mobile._call("POST", SCENE_RUN, {"sceneId": scene["scene_id"], "userId": mobile.user_id})
        else:
            super().run_scene(scene)

    def control(self, device, command, value=""):
        if device.get("backend") != "mobile":
            return super().control(device, command, value)
        body = control_body(device, command, value)
        if command not in device.get("mobile_commands", {}) and body.get("attribute") != "native_trait":
            fallback = device.get("agent_fallback")
            if fallback:
                return super().control(fallback, command, value)
            raise AqaraError("This device does not support that native command.")
        def current_scope():
            settings = self.read("settings")
            home = next((h for h in settings.get("homes", []) if h["id"] == settings.get("home_id")), {})
            return settings.get("home_id"), settings.get("home_links", {}).get(home.get("id"), home.get("mobile_id"))
        scope = current_scope()
        mobile = self.cloud_client()
        if device.get("model"):
            details = [row for row in objects(mobile.details(device["did"])) if str(row.get("did", "")) == device["did"]]
            if (len(details) != 1 or details[0].get("model") != device["model"] or not scope[1]
                    or str(details[0].get("homeId", "")) != scope[1]):
                raise AqaraError("This device's home or model changed. Run aq update before trying again.")
        # Re-read capabilities at execution, including exact endpoint, before any write.
        fresh_caps = (mobile.endpoint_capabilities(device["did"], device["model"])
                      if device.get("model") else mobile.endpoint_capabilities(device["did"]))
        fresh = next((c for c in fresh_caps[0]
                      if c["endpoint"] == device.get("endpoint")), {})
        if body.get("attribute") == "native_trait":
            spec = fresh.get("cloud_controls", {}).get(command)
            if not spec or spec != device.get("cloud_controls", {}).get(command):
                raise AqaraError("This device's capabilities changed. Run aq update before trying again.")
            self.ensure_current_mode()
            if self.active_profile_id() != self.profile_id or current_scope() != scope:
                raise AqaraError("The selected home changed. Select the device again.")
            try:
                mobile.write_trait(device["did"], spec, value)
            finally:
                (self.directory / "status.json").unlink(missing_ok=True)
            return body
        path = fresh.get("commands", {}).get(command)
        if not path or path != device.get("mobile_commands", {}).get(command):
            raise AqaraError("This lock's capabilities changed. Run aq update before trying again.")
        password = ("" if device.get("pwd_state") == 0 else
                    dialog("Enter this device's remote-control password, or leave blank if none is configured. "
                           "This is separate from your Aqara account password.", hidden=True))
        if password is None:
            raise AqaraError("Lock command cancelled.")
        self.ensure_current_mode()
        if self.active_profile_id() != self.profile_id or current_scope() != scope:
            raise AqaraError("The selected home changed. Select the lock again.")
        try:
            mobile.write_lock(device["did"], path, password)
        except CloudAPIError as exc:
            if exc.code == 1703:
                raise AqaraError("The device password was rejected. Check its remote-control password in Aqara Home. No retry was sent.") from None
            raise
        finally:
            (self.directory / "status.json").unlink(missing_ok=True)
        return body

    def import_agent(self, source=None):
        source = source or Store(getattr(self, "workflow_root", self.root.parent))
        if (self.root / "accounts.json").exists() or self.secrets():
            raise AqaraError("Agent import is only available before configuring the Cloud mode. Existing connections were kept.")
        snapshots = []
        for profile in source.profiles():
            original = source.for_profile(profile["id"])
            key = original.keychain.get()
            if not key:
                raise AqaraError("An Agent credential could not be read. Import was not completed.")
            snapshots.append((profile["id"], key, {name: original.read(name) for name in ("settings", "catalog", "catalogs")}))
        for identifier, key, files in snapshots:
            self.keychain_factory(identifier).set(json.dumps({"agent_key": key}))
            folder = self.root if identifier == "default" else self.root / "profiles" / identifier
            for name, data in files.items():
                if name == "settings":
                    for home in data.get("homes", []):
                        home["agent"] = True
                self._write_path(folder / (name + ".json"), data)
        self._write_path(self.root / "accounts.json", {"active_id": source.active_profile_id(), "profiles": source.profiles()})
        return "Copied Agent profiles into Cloud mode. Use aq login for each account to add Cloud lock capabilities."

    def replace_connection(self, value):
        previous_key = self.keychain.get()
        names = ("settings", "catalog", "catalogs", "status")
        previous = {name: self.read(name) if (self.directory / (name + ".json")).exists() else None
                    for name in names}
        try:
            self.keychain.set(json.dumps(value))
            self.discover_homes()
            self.clear_catalog()
        except Exception:
            if previous_key:
                self.keychain.set(previous_key)
            else:
                self.keychain.delete()
            for name, data in previous.items():
                if data is None:
                    (self.directory / (name + ".json")).unlink(missing_ok=True)
                else:
                    self.write(name, data)
            raise

    def connect(self, target=None):
        target = target or self
        identity = load_identity()
        area = dialog("Enter the Aqara Home region: US, SEA, EU, CN, or KR. India uses SEA.")
        if area is None:
            return "Connection cancelled."
        district = dialog("Enter the account country as two letters, such as US or IN.")
        if district is None:
            return "Connection cancelled."
        username = dialog("Enter your Aqara Home email address or phone number.")
        if username is None:
            return "Connection cancelled."
        password = dialog("Enter your Aqara Home account password. It is used once to create a Keychain session.", hidden=True)
        if password is None:
            return "Connection cancelled."
        client = BackendClient(identity, area.strip().upper(), district.strip().upper())
        client.login(username.strip(), password)
        client.catalog("homes")  # Validate before replacing an existing session.
        saved = target.secrets()
        saved.update(area=client.area, district=client.district, token=client.token,
                     user_id=client.user_id, signed_in=time.time())
        target.replace_connection(saved)
        return "Cloud session saved. Use aq home to select a home, then aq update."

    def handle_action(self, action):
        name = action.get("action")
        if name in ("mobile_home_link", "mobile_home_unlink"):
            if (action.get("profile_id") != self.profile_id or self.active_profile_id() != self.profile_id
                    or action.get("home_id") != self.read("settings").get("home_id")):
                raise AqaraError("The selected account or home changed. Choose the home link again.")
            if name == "mobile_home_link" and not isinstance(action.get("mobile_home_id"), str):
                raise AqaraError("Select a Cloud home to link.")
            return self.link_home(action["home_id"], action.get("mobile_home_id") if name == "mobile_home_link" else None)
        if name == "mobile_import":
            return self.import_agent()
        if name == "mobile_identity":
            result = subprocess.run(["/usr/bin/osascript", "-e", 'POSIX path of (choose file with prompt "Select an advanced Cloud signing override (cloud-identity.json or const.py)")'], capture_output=True, text=True)
            if result.returncode:
                return "Identity import cancelled."
            save_identity(result.stdout.strip())
            return "Cloud signing identity saved in Keychain. Use aq login to connect."
        if name == "login":
            return self.connect()
        if name == "account_add":
            label = self.validate_profile_name(action.get("name", ""))
            target = self.add_profile(label, "{}")
            try:
                message = self.connect(target)
                if not target.secrets().get("token"):
                    raise AqaraError(message)
                target.activate()
                return message
            except Exception:
                target.keychain.delete()
                profiles = [p for p in self.profiles() if p["id"] != target.profile_id]
                self._write_path(self.root / "accounts.json", {"active_id": self.active_profile_id(), "profiles": profiles})
                raise
        if name == "apikey":
            from command import prompt_key
            key = prompt_key()
            if key is None:
                return "Connection cancelled."
            Client(key).request("homes/query")
            saved = self.secrets()
            saved["agent_key"] = key
            self.replace_connection(saved)
            return "Agent fallback connected. Run aq update."
        return None

    def decorate_items(self, query, items):
        from filter import item
        term = query.strip().casefold()
        if term in ("home link", "home unlink"):
            settings = self.read("settings")
            current = next((h for h in settings.get("homes", []) if h["id"] == settings.get("home_id")), {})
            if not current.get("agent"):
                return [item("Choose an Agent home first", "Then use aq home link to select its Cloud counterpart", autocomplete="home ")]
            action = {"profile_id": self.profile_id, "home_id": current["id"]}
            if term == "home unlink":
                return [item("Remove this home link", current["name"], dict(action, action="mobile_home_unlink"))]
            choices = [h for h in settings.get("homes", []) if h.get("mobile_id") and not h.get("agent")]
            return [item("Link " + h["name"], "Keep Agent controls and add native devices in " + current["name"],
                         dict(action, action="mobile_home_link", mobile_home_id=h["mobile_id"])) for h in choices] or [
                item("No unlinked Cloud homes", "Use aq login and aq update first, or aq home unlink to change a link")]
        if term in ("home", "homes") and self.secrets().get("token"):
            items.append(item("Link a Cloud home…", "Associate different API IDs explicitly", autocomplete="home link"))
        if term == "parity":
            catalog = self.read("catalog")
            devices = catalog.get("devices", [])
            native = [d for d in devices if d.get("backend") == "mobile"]
            locks = [d for d in native if any(c in controls(d) for c in ("lock", "unlock"))]
            fallback = [d for d in native if d.get("agent_fallback")]
            native_controls = [d for d in native if d.get("cloud_controls")]
            return [item("Cloud command coverage", "%s endpoints advertise lock commands · Shared cloud route verified with U100 and U300" % len(locks)),
                    item("Agent fallback", "%s Cloud endpoints have an exact Agent match for additional commands" % len(fallback)),
                    item("Native device controls", "%s endpoints advertise common power, brightness, or color-temperature traits" % len(native_controls)),
                    item("Native coverage gaps", "Unmatched Agent devices stay in Agent mode; unknown Cloud controls remain read-only"),
                    item("Native scenes", "%s discovered · Native scenes use the app API; existing Agent scenes retain their route" % catalog.get("native_scene_count", 0)),
                    item("Partial metadata reads", "%s devices have an unavailable metadata route" % catalog.get("native_capability_failures", 0))]
        extra = [("import", "Import current Agent profiles", "Copy accounts and cached catalogs; keep Agent mode unchanged", "mobile_import"),
                 ("identity", "Override Cloud signing configuration", "Advanced only; normal login downloads and caches the verified configuration automatically", "mobile_identity"),
                 ("parity", "Compare Cloud coverage", "Show Cloud lock commands and Agent fallback coverage", None)]
        if term in ("import", "identity"):
            return [item(title, subtitle, {"action": action}) for key, title, subtitle, action in extra if key == term]
        for row in items:
            action = json.loads(row.get("arg", "{}"))
            if action.get("action") == "account_add":
                row["subtitle"] = "Sign in to this account's region using secure dialogs"
            if row.get("autocomplete") == "account add ":
                row["subtitle"] = "Save another Aqara Home account and region"
            if row["title"] == "Add an account or region…":
                row["subtitle"] = "Save another Aqara Home account and region"
            if action.get("action") == "login":
                row.update(title="Connect Cloud account", subtitle="Sign in securely to the Aqara Home Cloud API")
            if action.get("action") == "apikey":
                row.update(title="Connect Agent fallback", subtitle="Retain existing device controls and scene execution")
        if not term:
            items = [i for i in items if i["title"] != "Set up Aqara"]
            items.extend(item(title, subtitle, {"action": action} if action else None, key)
                         for key, title, subtitle, action in extra if key != "identity")
        return items
