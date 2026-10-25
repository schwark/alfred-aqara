"""Aqara Agent API, local catalog, and supported device controls (stdlib only)."""

from collections import Counter
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from keychain import Keychain, KeychainError

BUNDLE_ID = "com.schwark.aqara"
API_URL = "https://agent.aqara.com/open/api"
LOGIN_URL = "https://agent.aqara.com/login"
SUCCESS_CODES = (0, 200, "0", "200", "OK", "SUCCESS", "success", None)


class AqaraError(Exception):
    pass


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the account credential to a redirect target.
        return None


class Client:
    def __init__(self, key, home_id="", opener=None):
        if not key:
            raise AqaraError("Connect your account with aq apikey first.")
        self.key = key
        self.home_id = home_id
        self.opener = opener or build_opener(NoRedirects())

    def request(self, path, body=None):
        headers = {"Authorization": "Bearer " + self.key,
                   "application_id": "AqaraAgentSkills", "lang": "en",
                   "Accept": "application/json"}
        if path != "homes/query":
            if not self.home_id:
                raise AqaraError("Choose a home with aq home first.")
            headers["position_id"] = self.home_id
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(API_URL + "/" + path, data=data, headers=headers)
        try:
            with self.opener.open(req, timeout=15) as response:
                result = json.load(response)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status in (401, 403):
                raise AqaraError("Aqara authorization failed. Use aq login, then aq apikey to reconnect.") from None
            if status == 429:
                raise AqaraError("Aqara is rate limiting requests. Wait a moment and try again.") from None
            raise AqaraError("Aqara returned HTTP %s. Try again later." % status) from None
        except (URLError, TimeoutError, socket.timeout, OSError):
            raise AqaraError("Cannot reach Aqara. Check your connection and try again. A timed-out command may still have run.") from None
        except (ValueError, UnicodeError):
            raise AqaraError("Aqara returned an unreadable response.") from None
        # Agent uses an error envelope for a valid home with no saved scenes.
        # Match the observed empty-result response narrowly; other failures remain errors.
        if (path == "home/scenes/query" and isinstance(result, dict)
                and result.get("code") in (-1, "-1") and result.get("result") in ({}, [])
                and re.fullmatch(r"(?:Scene base inquiry failed:\s*)?No scenes? found at home[.!]?",
                                 str(result.get("message", "")).strip(), re.IGNORECASE)):
            return {"code": 0, "result": []}
        try:
            check_result(result)
        except AqaraError as exc:
            context = {"homes/query": "Finding homes", "home/devices/query": "Loading devices",
                       "home/scenes/query": "Loading scenes", "device/status/query": "Reading status",
                       "device/control": "Controlling device", "scene/run": "Running scene"}.get(path, "Aqara request")
            raise AqaraError(context + ": " + str(exc)) from None
        return result


def rejection_message(value):
    """Translate known failures without displaying arbitrary server content."""
    details = " ".join(str(value.get(key, "")) for key in ("message", "msg", "error"))
    if "nohomesunderaccount" in details.casefold():
        return ("Aqara Agent cannot see any homes for this account (NoHomesUnderAccount). "
                "Check that Aqara Home and Aqara Agent use the same account and region, then reconnect.")
    if any(reason in details.casefold() for reason in
           ("unauthorized", "insufficient permissions", "invalid token", "token expired", "invalid api key")):
        return "Aqara authorization failed. Use aq login, then aq apikey to reconnect."
    code = str(value.get("code", ""))
    # Only numeric error codes are safe to display; never echo server messages,
    # which can contain account details, tokens, or request headers.
    suffix = " (code %s)" % code if re.fullmatch(r"-?\d{1,10}", code) else ""
    return "Aqara rejected the request%s. Check your account permissions or reconnect with aq login and aq apikey." % suffix


def check_result(value):
    """Check transport-independent failures without echoing server credentials."""
    if not isinstance(value, (dict, list)):
        raise AqaraError("Aqara returned an unexpected response.")
    if isinstance(value, dict):
        if value.get("code") not in SUCCESS_CODES or value.get("success") is False:
            raise AqaraError(rejection_message(value))
        if value.get("status") in ("failed", "error", "failure") or value.get("error"):
            raise AqaraError(rejection_message(value))
        for key in ("data", "result", "results"):
            if isinstance(value.get(key), (list, dict)):
                check_result(value[key])
    else:
        for item in value:
            if isinstance(item, (dict, list)):
                check_result(item)


def table_rows(value):
    """Aqara Agent returns tables as [[column names], [values], ...]."""
    if not value or not isinstance(value[0], list):
        return value
    headers = value[0]
    if not headers or not all(isinstance(header, str) and header.strip() for header in headers):
        raise AqaraError("Aqara returned a table without valid column names.")
    headers = [re.sub(r"[\s-]+", "_", header.strip().casefold()) for header in headers]
    if len(set(headers)) != len(headers):
        raise AqaraError("Aqara returned a table with duplicate column names.")
    result = []
    for row in value[1:]:
        if not isinstance(row, list) or len(row) != len(headers):
            raise AqaraError("Aqara returned a table row with missing or extra values.")
        result.append(dict(zip(headers, row)))
    return result


def rows(payload, kind):
    """Unwrap documented Aqara envelopes; reject unknown shapes, keep old cache."""
    keys = {"homes": ("homes", "positions", "list", "items"),
            "devices": ("home_devices", "devices", "list", "items"),
            "scenes": ("home_scenes", "scenes", "list", "items")}[kind]
    if isinstance(payload, list):
        return table_rows(payload)
    if isinstance(payload, dict):
        for key in keys + ("data", "result"):
            if key in payload and isinstance(payload[key], (list, dict)):
                try:
                    return rows(payload[key], kind)
                except AqaraError:
                    continue
    raise AqaraError("Unrecognized Aqara %s response. The previous catalog has been kept." % kind)


def field(row, *names):
    for name in names:
        if row.get(name) is not None and row[name] != "":
            return str(row[name])
    return ""


def normalize(payload, kind):
    result = []
    seen = set()
    for row in rows(payload, kind):
        if not isinstance(row, dict):
            raise AqaraError("Unexpected %s entry from Aqara." % kind)
        if kind == "homes":
            identifier = field(row, "positionId", "homeId", "id", "position_id", "home_id")
            name = field(row, "positionName", "homeName", "name", "home_name")
            item = {"id": identifier, "name": name or identifier}
        else:
            identifier = (field(row, "endpoint_id", "device_id", "deviceId", "did", "id")
                          if kind == "devices" else field(row, "scene_id", "sceneId", "id"))
            name = field(row, "endpoint_name", "endpointName", "device_name", "deviceName", "scene_name", "sceneName", "name")
            item = {"id": identifier, "name": name or identifier,
                    "device_name": field(row, "device_name", "deviceName"),
                    "room": field(row, "position_name", "positionName", "room_name", "roomName"),
                    "type": field(row, "device_type", "deviceType", "type"),
                    "kind": "device" if kind == "devices" else "scene"}
        if not identifier:
            raise AqaraError("Aqara returned a %s entry without an ID. The catalog has been kept." % kind)
        if identifier not in seen:
            seen.add(identifier)
            result.append(item)
    result.sort(key=lambda item: (item.get("room", "").casefold(), item["name"].casefold(), item["id"]))
    if kind == "devices":
        names = Counter(item["name"].casefold() for item in result)
        labels = Counter()
        for item in result:
            label = item["name"]
            if names[label.casefold()] > 1:
                label += " (" + (item["room"] or "No room") + ")"
            labels[label.casefold()] += 1
            item["label"] = label
        counts = Counter()
        for item in result:
            label = item["label"]
            if labels[label.casefold()] > 1:
                counts[label.casefold()] += 1
                item["label"] += " · " + str(counts[label.casefold()])
    return result


def refresh_in_progress(store):
    state = store.read("refresh")
    pid = state.get("pid")
    if state.get("state") != "running" or type(pid) is not int or not 0 < pid < 2**31:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class Store:
    mode = "agent"

    def __init__(self, directory=None, keychain=None, profile_id=None, keychain_factory=None):
        fallback = Path.home() / "Library/Application Support/Alfred/Workflow Data" / BUNDLE_ID
        self.root = Path(directory or os.environ.get("alfred_workflow_data") or fallback)
        accounts = self._read_path(self.root / "accounts.json")
        self.profile_id = profile_id if profile_id is not None else accounts.get("active_id", "default")
        profiles = accounts.get("profiles", [{"id": "default", "name": "Current account"}])
        if self.profile_id not in {profile["id"] for profile in profiles}:
            raise AqaraError("Account profile not found. Choose one with aq account.")
        self.directory = self.root if self.profile_id == "default" else self.root / "profiles" / self.profile_id
        self.keychain_factory = keychain_factory or (
            lambda identifier: keychain if keychain is not None and identifier == self.profile_id
            else Keychain(account="api-key" if identifier == "default" else "api-key:" + identifier))
        self.keychain = self.keychain_factory(self.profile_id)

    @staticmethod
    def _read_path(path):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def read(self, name):
        return self._read_path(self.directory / (name + ".json"))

    @staticmethod
    def _write_path(path, value):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, filename = tempfile.mkstemp(prefix=".aqara-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False)
                stream.write("\n")
            os.replace(filename, path)
        finally:
            if os.path.exists(filename):
                os.unlink(filename)

    def write(self, name, value):
        self._write_path(self.directory / (name + ".json"), value)

    def profiles(self):
        return self._read_path(self.root / "accounts.json").get(
            "profiles", [{"id": "default", "name": "Current account"}])

    def profile_name(self):
        return next(profile["name"] for profile in self.profiles() if profile["id"] == self.profile_id)

    def active_profile_id(self):
        return self._read_path(self.root / "accounts.json").get("active_id", "default")

    def for_profile(self, identifier):
        target = type(self)(self.root, profile_id=identifier, keychain_factory=self.keychain_factory)
        for name in ("workflow_root", "mode_revision", "mode_guard"):
            if hasattr(self, name):
                setattr(target, name, getattr(self, name))
        return target

    def ensure_current_mode(self):
        guard = getattr(self, "mode_guard", None)
        if guard:
            guard()

    def activate(self):
        self._write_path(self.root / "accounts.json", {"active_id": self.profile_id, "profiles": self.profiles()})

    def validate_profile_name(self, name, renaming=False):
        name = name.strip()
        if not name or len(name) > 64:
            raise AqaraError("Enter an account name between 1 and 64 characters.")
        if (re.search(r"\b(?:bearer\s+|api[_-]?key\s*[:=]|token\s*[:=])", name, re.IGNORECASE)
                or any(any(char.isdigit() for char in token) and any(char.isalpha() for char in token)
                       for token in re.findall(r"[A-Za-z0-9_-]{24,}", name))):
            raise AqaraError("Enter only an account or region name. Paste the API key into the secure key dialog.")
        if any(profile["name"].casefold() == name.casefold()
               and (not renaming or profile["id"] != self.profile_id) for profile in self.profiles()):
            raise AqaraError("An account with that name already exists. Choose a different name.")
        return name

    def add_profile(self, name, key):
        name = self.validate_profile_name(name)
        if key and key.strip() in name:
            raise AqaraError("The account name contains the API key. Choose a label such as Personal or Work.")
        identifier = uuid.uuid4().hex
        self.keychain_factory(identifier).set(key)
        profiles = self.profiles() + [{"id": identifier, "name": name}]
        self._write_path(self.root / "accounts.json", {"active_id": self.active_profile_id(), "profiles": profiles})
        target = self.for_profile(identifier)
        target.write("settings", {"showstatus": self.read("settings").get("showstatus", False)})
        return target

    def rename_profile(self, name):
        name = self.validate_profile_name(name, renaming=True)
        profiles = self.profiles()
        for profile in profiles:
            if profile["id"] == self.profile_id:
                profile["name"] = name
        self._write_path(self.root / "accounts.json", {"active_id": self.active_profile_id(), "profiles": profiles})
        return name

    def clear_catalog(self):
        for name in ("catalog", "catalogs", "status", "refresh"):
            (self.directory / (name + ".json")).unlink(missing_ok=True)

    def client(self):
        return Client(self.keychain.get(), self.read("settings").get("home_id", ""))

    def discover_homes(self, client=None):
        client = client or self.client()
        homes = normalize(client.request("homes/query"), "homes")
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
        allowed = {home["id"] for home in homes}
        cached = {identifier: catalog for identifier, catalog in self.read("catalogs").items() if identifier in allowed}
        self.write("catalogs", cached)
        return homes

    def refresh(self, client=None):
        client = client or self.client()
        homes = self.discover_homes(client)
        settings = self.read("settings")
        if not settings.get("home_id"):
            raise AqaraError("Choose a home with aq home, then run aq update." if homes
                             else "No homes found in this Aqara account.")
        client.home_id = settings["home_id"]
        devices = normalize(client.request("home/devices/query"), "devices")
        scenes = normalize(client.request("home/scenes/query"), "scenes")
        catalog = {"home_id": client.home_id, "updated": time.time(), "devices": devices, "scenes": scenes}
        self.write("catalog", catalog)
        cached = self.read("catalogs")
        cached[client.home_id] = catalog
        self.write("catalogs", cached)
        return len(devices), len(scenes)

    def select_home(self, identifier):
        settings = self.read("settings")
        home = next((h for h in settings.get("homes", []) if h["id"] == identifier), None)
        if home is None:
            raise AqaraError("Home not found. Run aq update to refresh the home list.")
        cached = self.read("catalogs")
        current = self.read("catalog")
        if current.get("home_id") == settings.get("home_id") and current.get("home_id"):
            cached[current["home_id"]] = current
            self.write("catalogs", cached)
        settings.update(home_id=home["id"], home_name=home["name"])
        self.write("settings", settings)
        if cached.get(identifier, {}).get("home_id") == identifier:
            self.write("catalog", cached[identifier])
        else:
            (self.directory / "catalog.json").unlink(missing_ok=True)
        (self.directory / "status.json").unlink(missing_ok=True)
        return home["name"]

    def switch_home(self, identifier):
        """Keep the previous home usable if loading the new catalog fails."""
        names = ("settings", "catalog", "catalogs", "status")
        previous = {name: self.read(name) if (self.directory / (name + ".json")).exists() else None
                    for name in names}
        try:
            home = self.select_home(identifier)
            catalog = self.read("catalog")
            if catalog.get("home_id") != identifier:
                self.refresh()
                catalog = self.read("catalog")
        except (AqaraError, KeychainError, OSError):
            for name, value in previous.items():
                if value is None:
                    (self.directory / (name + ".json")).unlink(missing_ok=True)
                else:
                    self.write(name, value)
            raise
        return home, catalog

    def control(self, device, command, value=""):
        body = control_body(device, command, value)
        client = self.client()
        self.ensure_current_mode()
        client.request("device/control", body)
        return body

    def run_scene(self, scene):
        client = self.client()
        self.ensure_current_mode()
        client.request("scene/run", {"scene_ids": [scene["id"]]})

    def status(self, device, client=None):
        cache = self.read("status")
        key = self.read("settings").get("home_id", "") + ":" + device["id"]
        entry = cache.get(key, {})
        if time.time() - entry.get("updated", 0) < 5:
            return entry["data"]
        data = (client or self.client()).request("device/status/query", {"device_ids": [device["id"]]})
        cache[key] = {"updated": time.time(), "data": data}
        self.write("status", cache)
        return data


def selector(device):
    return device.get("label", device["name"])


def controls(device):
    """Map API device families to the official Aqara control action table."""
    if device.get("backend") == "mobile":
        from cloud_capabilities import control_hint
        commands = controls(device["agent_fallback"]) if device.get("agent_fallback") else {"status": (None, None, None)}
        for command, spec in device.get("cloud_controls", {}).items():
            commands[command] = ("native_trait", command, commands[command][2] if command in commands else control_hint(spec))
        for command in ("lock", "unlock"):
            if command in device.get("mobile_commands", {}):
                commands[command] = ("lock_state", command, None)
        return commands
    kind = device.get("type", "").casefold()
    commands = {"status": (None, None, None)}
    if "doorlock" in kind:
        return commands
    if any(token in kind for token in ("light", "switch", "outlet", "airconditioner", "plug")):
        commands.update(on=("on_off", "on", None), off=("on_off", "off", None))
    if "light" in kind:
        commands.update(dim=("brightness", "set", "1–100"),
                        color=("color", "set", "color name"),
                        ct=("color_temperature", "set", "2700–6500 K"))
    if any(token in kind for token in ("windowcovering", "clothedryingmachine")):
        commands.update(open=("percentage", "set", "100"), close=("percentage", "set", "0"),
                        stop=("motion", "set", "stop"), slevel=("percentage", "set", "0–100"))
    if "airconditioner" in kind:
        commands.update(mode=("ac_mode", "set", "auto, heat, cool, dry, fan"),
                        temp=("temperature", "set", "18–30 °C"))
    return commands


def control_body(device, command, value=""):
    available = controls(device)
    if command not in available or command == "status":
        raise AqaraError("This device does not support that command.")
    attribute, action, fixed = available[command]
    if device.get("backend") == "mobile" and command in device.get("cloud_controls", {}):
        from cloud_capabilities import validate_control
        try:
            wire_value = validate_control(device["cloud_controls"][command], value)
        except AqaraError:
            # Select a compatible transport before sending anything. Never
            # retry a failed native write through Agent.
            if device.get("agent_fallback") and command in controls(device["agent_fallback"]):
                return control_body(device["agent_fallback"], command, value)
            raise
        return {"device_ids": [device["id"]], "attribute": attribute, "action": action, "value": wire_value}
    if command in ("dim", "ct", "slevel", "temp"):
        bounds = {"dim": (1, 100), "ct": (2700, 6500), "slevel": (0, 100), "temp": (18, 30)}
        low, high = bounds[command]
        try:
            number = int(value)
        except ValueError:
            raise AqaraError("Enter a whole number from %s to %s." % (low, high)) from None
        if not low <= number <= high:
            raise AqaraError("Enter a value from %s to %s." % (low, high))
        value = str(number)
    elif command == "mode":
        value = value.casefold()
        if value not in ("auto", "heat", "cool", "dry", "fan"):
            raise AqaraError("Choose auto, heat, cool, dry, or fan.")
    elif command == "color":
        value = value.strip().casefold()
        if not value or len(value) > 32 or not all(c.isalpha() or c in " -" for c in value):
            raise AqaraError("Enter a color name, such as red or blue.")
    elif value:
        raise AqaraError("This command does not accept a value.")
    else:
        value = fixed
    body = {"device_ids": [device["id"]], "attribute": attribute, "action": action}
    if value is not None:
        body["value"] = value
    return body
