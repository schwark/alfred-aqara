"""Experimental mobile connection, with explicit Agent fallback for feature parity.

Device writes use only the common, capability-advertised Matter lock commands. Unknown native controls remain read-only. No model-name dispatch,
automatic write retry, legacy pairing-key extraction, or guessed resource writes.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from common import AqaraError, Client, Store, control_body, controls, normalize
from keychain import Keychain
from mobile_transport import ALLOWED, AppIdentity, MobileAPIError, MobileClient


BUNDLE = "com.schwark.aqara.mobile"
SCENES = "/app/position/scene/query"
MATTER_WRITE = "/matter/write"
SCENE_RUN = "/ifttt/scene/run"


def identity_key():
    return Keychain(service=BUNDLE, account="app-identity")


def save_identity(reference):
    identity = AppIdentity.from_reference(reference)
    identity_key().set(json.dumps({"app_id": identity.app_id, "app_key": identity.app_key,
                                  "modulus": identity.modulus, "exponent": identity.exponent,
                                  "version": identity.version}))


def load_identity():
    try:
        value = json.loads(identity_key().get() or "{}")
        identity = AppIdentity(**value)
        if (not isinstance(identity.app_id, str) or not re.fullmatch(r"[A-Za-z0-9]{8,64}", identity.app_id)
                or not isinstance(identity.app_key, str) or not 16 <= len(identity.app_key) <= 128
                or type(identity.modulus) is not int or not 1024 <= identity.modulus.bit_length() <= 8192
                or identity.exponent != 65537):
            raise ValueError()
        return identity
    except (ValueError, TypeError):
        raise AqaraError("Import the mobile app identity with aqm identity first.") from None


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
    raise AqaraError("The mobile API returned an unexpected %s catalog. Previous data was kept." % kind)


class BackendClient(MobileClient):
    routes = ALLOWED | {("GET", SCENES), ("POST", MATTER_WRITE), ("POST", SCENE_RUN)}

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
                raise AqaraError("The mobile API repeated a page or omitted an identifier. Previous data was kept.")
            result.extend(page)
            seen.update(ids)
            if count is not None and len(result) > count:
                raise AqaraError("The mobile API returned inconsistent page counts. Previous data was kept.")
            if count is not None and len(result) == count:
                return result
            if len(page) < 300:
                if count is not None and len(result) < count:
                    raise AqaraError("The mobile API returned an incomplete catalog. Previous data was kept.")
                return result
        raise AqaraError("The mobile catalog exceeds the supported page limit. Previous data was kept.")

    def write_lock(self, did, path, password=""):
        # Exactly the shared Android request builder, with an explicit single write.
        if not re.fullmatch(r"\d+\.148\.(35010|35011)\.0", path):
            raise AqaraError("The selected device did not advertise a supported lock command.")
        return self._call("POST", MATTER_WRITE, {"did": did,
            "pwd": hashlib.md5(password.encode("utf-8")).hexdigest() if password else "",
            "data": {path: ""}, "type": 0})


def capabilities(payload, did):
    """Bind commands to this device's endpoints, never the global command list."""
    result = []
    if not isinstance(payload, dict) or not isinstance(payload.get("devices"), list):
        return result
    matches = [d for d in payload["devices"] if isinstance(d, dict) and str(d.get("did", "")) == did]
    if len(matches) != 1:
        return result
    def objects(value):
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
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


def dialog(message, title="Aqara Mobile", hidden=False):
    script = ('text returned of (display dialog ' + json.dumps(message) + ' with title ' + json.dumps(title)
              + ' default answer "" ' + ('with hidden answer ' if hidden else '')
              + 'buttons {"Cancel", "Continue"} default button "Continue")')
    result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True)
    return result.stdout.rstrip("\n") if result.returncode == 0 else None


class MobileStore(Store):
    is_mobile = True

    def __init__(self, directory=None, keychain=None, profile_id=None, keychain_factory=None):
        directory = directory or os.environ.get("alfred_workflow_data") or (
            Path.home() / "Library/Application Support/Alfred/Workflow Data" / BUNDLE)
        factory = keychain_factory or (lambda identifier: Keychain(service=BUNDLE, account="connection:" + identifier))
        super().__init__(directory, keychain, profile_id, factory)

    def secrets(self):
        try:
            value = json.loads(self.keychain.get() or "{}")
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
        raise AqaraError("Reconnect this mobile account with aqm login.")

    def mobile_client(self):
        value = self.secrets()
        if not value.get("token") or not value.get("user_id"):
            raise AqaraError("Connect the mobile account with aqm login first.")
        client = BackendClient(load_identity(), value.get("area", ""), value.get("district", ""))
        client.token, client.user_id = value["token"], value["user_id"]
        return client

    def client(self):
        value = self.secrets()
        if not value.get("agent_key"):
            raise AqaraError("This action needs the Agent fallback. Use aqm apikey or aqm import.")
        home = self.read("settings").get("home_id", "")
        return Client(value["agent_key"], home)

    def discover_homes(self, client=None):
        value, homes = self.secrets(), []
        if value.get("agent_key"):
            for home in normalize((client or self.client()).request("homes/query"), "homes"):
                homes.append(dict(home, agent=True))
        if value.get("token"):
            for row in self.mobile_client().catalog("homes"):
                identifier = str(row.get("homeId") or row["positionId"])
                existing = next((home for home in homes if home["id"] == identifier), None)
                if existing:
                    existing["mobile_id"] = identifier
                else:
                    name = str(row.get("homeName") or row.get("positionName") or "Home")
                    if any(home["name"].casefold() == name.casefold() for home in homes):
                        name += " (Mobile)"
                    homes.append({"id": "mobile:" + identifier, "mobile_id": identifier, "name": name})
        if not homes:
            raise AqaraError("No homes found. Connect the mobile account with aqm login or import Agent profiles.")
        settings = self.read("settings")
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

    def refresh(self, client=None):
        self.discover_homes(client)
        settings = self.read("settings")
        home = next((h for h in settings["homes"] if h["id"] == settings.get("home_id")), None)
        if not home:
            raise AqaraError("Choose a home with aqm home, then run aqm update.")
        devices, scenes = [], []
        if home.get("agent"):
            agent = client or self.client()
            agent.home_id = home["id"]
            devices = normalize(agent.request("home/devices/query"), "devices")
            scenes = normalize(agent.request("home/scenes/query"), "scenes")
        native_failures = 0
        if home.get("mobile_id"):
            mobile = self.mobile_client()
            for row in mobile.catalog("devices", home["mobile_id"]):
                did = str(row["did"])
                # Query common capabilities for every device. No model whitelist.
                try:
                    caps = capabilities(mobile.matter_details(did), did)
                except MobileAPIError:
                    caps = []
                    native_failures += 1
                endpoints = caps or [{"endpoint": "", "commands": {}, "readings": {}}]
                for cap in endpoints:
                    name = str(row.get("deviceName") or row.get("originalName") or "Device")
                    if len(endpoints) > 1:
                        name += " · Endpoint " + cap["endpoint"]
                    device = {"id": "mobile:" + did + ("#" + cap["endpoint"] if cap["endpoint"] else ""),
                              "did": did, "endpoint": cap["endpoint"], "name": name, "device_name": name,
                              "room": str(row.get("positionName") or ""), "kind": "device", "backend": "mobile",
                              "type": "DoorLock" if caps or ".lock." in str(row.get("model", "")).casefold() else str(row.get("model") or "Device"),
                              "mobile_commands": cap["commands"], "mobile_readings": cap["readings"],
                              "lock_note": "No shared lock command advertised; legacy pairing control is not implemented"}
                    # Merge only an exact identifier; never associate a write by name.
                    existing = next((d for d in devices if d["id"] == did), None)
                    if existing and caps and len(endpoints) == 1:
                        existing.update({k: v for k, v in device.items() if k not in ("id", "name", "room", "device_name")})
                    else:
                        if any(d["name"].casefold() == name.casefold() for d in devices):
                            device["name"] += " (Mobile)"
                        devices.append(device)
            native_scenes = mobile.catalog("scenes", home["mobile_id"])
            for row in native_scenes:
                identifier = str(row["sceneId"])
                if not any(scene["id"] == identifier for scene in scenes):
                    scenes.append({"id": "mobile:" + identifier, "scene_id": identifier, "backend": "mobile",
                                   "name": str(row.get("sceneName") or row.get("name") or "Scene"),
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
        mobile = client or self.mobile_client()
        paths = device.get("mobile_readings", {})
        data = readings(mobile.matter_status(device["did"], paths) if paths else mobile.status(device["did"]), paths)
        cache[key] = {"updated": time.time(), "data": data}
        self.write("status", cache)
        return data

    def run_scene(self, scene):
        if scene.get("backend") == "mobile":
            mobile = self.mobile_client()
            mobile._call("POST", SCENE_RUN, {"sceneId": scene["scene_id"], "userId": mobile.user_id})
        else:
            super().run_scene(scene)

    def control(self, device, command, value=""):
        if device.get("backend") != "mobile":
            return super().control(device, command, value)
        body = control_body(device, command, value)
        scope = self.read("settings").get("home_id")
        mobile = self.mobile_client()
        # Re-read capabilities at execution, including exact endpoint, before any write.
        fresh = next((c for c in capabilities(mobile.matter_details(device["did"]), device["did"])
                      if c["endpoint"] == device.get("endpoint")), {})
        path = fresh.get("commands", {}).get(command)
        if not path or path != device.get("mobile_commands", {}).get(command):
            raise AqaraError("This lock's capabilities changed. Run aqm update before trying again.")
        password = dialog("Enter this device's remote-control password, or leave blank if none is configured. "
                          "This is separate from your Aqara account password.", hidden=True)
        if password is None:
            raise AqaraError("Lock command cancelled.")
        if self.active_profile_id() != self.profile_id or self.read("settings").get("home_id") != scope:
            raise AqaraError("The selected home changed. Select the lock again.")
        try:
            mobile.write_lock(device["did"], path, password)
        except MobileAPIError as exc:
            if exc.code == 1703:
                raise AqaraError("The device password was rejected. Check its remote-control password in Aqara Home. No retry was sent.") from None
            raise
        finally:
            (self.directory / "status.json").unlink(missing_ok=True)
        return body

    def import_agent(self, source=None):
        source = source or Store(Path.home() / "Library/Application Support/Alfred/Workflow Data/com.schwark.aqara")
        if (self.root / "accounts.json").exists() or self.secrets():
            raise AqaraError("Agent import is only available before configuring the parallel workflow. Existing connections were kept.")
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
        return "Copied Agent profiles into Aqara Mobile. Use aqm login for each account to add mobile lock capabilities."

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
        return "Mobile session saved. Use aqm home to select a home, then aqm update."

    def handle_action(self, action):
        name = action.get("action")
        if name == "mobile_import":
            return self.import_agent()
        if name == "mobile_identity":
            result = subprocess.run(["/usr/bin/osascript", "-e", 'POSIX path of (choose file with prompt "Select the external app identity reference (const.py)")'], capture_output=True, text=True)
            if result.returncode:
                return "Identity import cancelled."
            save_identity(result.stdout.strip())
            return "Mobile app identity saved in Keychain. Use aqm login to connect."
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
            return "Agent fallback connected. Run aqm update."
        return None

    def decorate_items(self, query, items):
        from filter import item
        term = query.strip().casefold()
        if term == "parity":
            catalog = self.read("catalog")
            devices = catalog.get("devices", [])
            native = [d for d in devices if d.get("backend") == "mobile"]
            locks = [d for d in native if any(c in controls(d) for c in ("lock", "unlock"))]
            fallback = [d for d in devices if d.get("backend") != "mobile"]
            return [item("Mobile command coverage", "%s endpoints advertise lock commands · Execution remains unverified until you test" % len(locks)),
                    item("Agent fallback", "%s endpoints retain their existing commands and scenes" % len(fallback)),
                    item("Native coverage gaps", "Other mobile devices currently have status only; legacy encrypted locks need pairing support"),
                    item("Native scenes", "%s discovered · Native scenes use the app API; existing Agent scenes retain their route" % catalog.get("native_scene_count", 0)),
                    item("Capability reads unavailable", "%s devices · No control capability was guessed" % catalog.get("native_capability_failures", 0))]
        extra = [("import", "Import current Agent profiles", "Copy accounts and cached catalogs; keep the aq workflow separate", "mobile_import"),
                 ("identity", "Import mobile app identity", "Read a separately obtained constant reference into Keychain", "mobile_identity"),
                 ("parity", "Compare mobile coverage", "Show mobile lock commands and Agent fallback coverage", None)]
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
                row.update(title="Connect mobile account", subtitle="Sign in securely to the Aqara Home mobile API")
            if action.get("action") == "apikey":
                row.update(title="Connect Agent fallback", subtitle="Retain existing device controls and scene execution")
            row["subtitle"] = row.get("subtitle", "").replace("aq ", "aqm ")
        if not term:
            items = [i for i in items if i["title"] != "Set up Aqara"]
            items.extend(item(title, subtitle, {"action": action} if action else None, key)
                         for key, title, subtitle, action in extra)
        return items
