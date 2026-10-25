"""Shared signed transport for the Aqara Cloud backend.

Wire-format reference: home-assistant-plugins/aquara-local at
4fc274688cd40774307549172c9ae1db073c92ca (MIT). Shared app constants are fetched
automatically from the verified reference and cached in Keychain. Credentials and
sessions stay in memory. Runtime storage and capability-bound writes are
implemented separately in cloud_backend.
"""

import ast
import base64
from dataclasses import dataclass, field
import hashlib
import gzip
import io
import json
from pathlib import Path
import re
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from common import AqaraError


# Regional SDK routing: https://opendoc.aqara.com/docs/SDK开发/Android开发指南/地区对照表.html
HOSTS = {"SEA": "https://rpc-au.aqara.com", "US": "https://aiot-rpc-usa.aqara.com",
         "EU": "https://rpc-ger.aqara.com", "KR": "https://rpc-kr.aqara.com",
         "CN": "https://aiot-rpc.ankasa.cn"}
PREFIX = "/app/v1.0/lumi"
LOGIN = "/user/guard-code/login"
HOMES = "/app/position/query/home/list"
DEVICES = "/app/position/device/query"
DETAIL = "/app/dev/query/detail"
RESOURCES = "/res/query"
PLUGINS = "/app/rn/latest/list"
MATTER_DETAIL = "/matter/dev/detail"
MATTER_RESOURCES = "/res/query/by/resourceId"
TRAIT_READ = "/app/qlink/trait/read"
PANELS = "/app/layout/collection/panels"
SPEC = "/app/aqara/spec/config"
GATEWAY_DETAIL = "/dev/query/gateway/detail"
BRIDGE_GATEWAYS = "/app/dev/matter/bridge"
BRIDGE_DEVICES = "/app/dev/matter/bridge/subdevice"
DEFINITION_URL = "/app/qlink/specf/config/url"
ALLOWED = {("POST", LOGIN), ("GET", HOMES), ("GET", DEVICES),
           ("GET", DETAIL), ("POST", RESOURCES), ("GET", PLUGINS),
           ("GET", MATTER_DETAIL), ("POST", MATTER_RESOURCES),
           ("POST", TRAIT_READ), ("GET", PANELS), ("GET", SPEC),
           ("GET", GATEWAY_DETAIL), ("POST", BRIDGE_GATEWAYS), ("POST", BRIDGE_DEVICES),
           ("GET", DEFINITION_URL)}
MAX_RESPONSE = 4 * 1024 * 1024


class CloudAPIError(AqaraError):
    """Safe diagnostic with an optional numeric server code, never a response body."""

    def __init__(self, message, code=None):
        self.code = code
        super().__init__(message)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True, repr=False)
class AppIdentity:
    app_id: str
    app_key: str = field(repr=False)
    modulus: int = field(repr=False)
    exponent: int = 65537
    version: str = "6.1.6"

    def payload(self):
        return {"app_id": self.app_id, "app_key": self.app_key, "modulus": self.modulus,
                "exponent": self.exponent, "version": self.version}

    @classmethod
    def from_payload(cls, value):
        try:
            if not isinstance(value, dict) or set(value) - {"app_id", "app_key", "modulus", "exponent", "version"}:
                raise ValueError()
            identity = cls(**value)
            if (not isinstance(identity.app_id, str) or not re.fullmatch(r"[A-Za-z0-9]{8,64}", identity.app_id)
                    or not isinstance(identity.app_key, str) or not 16 <= len(identity.app_key) <= 128
                    or type(identity.modulus) is not int or not 1024 <= identity.modulus.bit_length() <= 8192
                    or identity.exponent != 65537 or not isinstance(identity.version, str)
                    or not re.fullmatch(r"\d+(?:\.\d+){1,3}", identity.version)):
                raise ValueError()
            return identity
        except (TypeError, ValueError):
            raise CloudAPIError("The Cloud signing configuration is missing or invalid.") from None

    @classmethod
    def from_json(cls, filename):
        try:
            with Path(filename).open("rb") as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError()
            return cls.from_payload(json.loads(raw))
        except (OSError, ValueError, UnicodeError):
            raise CloudAPIError("The Cloud signing configuration is missing or invalid.") from None

    @classmethod
    def from_reference(cls, filename):
        """Read only literal constants; imports and other Python are never executed."""
        try:
            return cls.from_reference_text(Path(filename).read_text())
        except (OSError, UnicodeError):
            raise CloudAPIError("The Cloud signing reference constants are missing or invalid.") from None

    @classmethod
    def from_reference_text(cls, source):
        """Treat downloaded Python as data; never import or execute it."""
        try:
            values = {}
            for node in ast.parse(source).body:
                if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                    continue
                name = node.targets[0].id
                if name in ("APPID", "APPKEY", "RSA_E"):
                    values[name] = ast.literal_eval(node.value)
                elif name == "RSA_N":
                    expression = node.value
                    if (isinstance(expression, ast.Call) and isinstance(expression.func, ast.Name)
                            and expression.func.id == "int" and len(expression.args) == 2
                            and not expression.keywords and ast.literal_eval(expression.args[1]) == 16):
                        values[name] = int(ast.literal_eval(expression.args[0]), 16)
                    else:
                        values[name] = ast.literal_eval(expression)
            return cls.from_payload({"app_id": values["APPID"], "app_key": values["APPKEY"],
                                     "modulus": values["RSA_N"], "exponent": values["RSA_E"]})
        except (OSError, ValueError, TypeError, KeyError, SyntaxError):
            raise CloudAPIError("The Cloud signing reference constants are missing or invalid.") from None


def encrypt_password(password, identity):
    message = hashlib.md5(password.encode("utf-8")).hexdigest().encode("ascii")
    width = (identity.modulus.bit_length() + 7) // 8
    padding_length = width - len(message) - 3
    if padding_length < 8:
        raise CloudAPIError("Invalid Cloud signing encryption key.")
    padding = bytearray()
    while len(padding) < padding_length:
        padding.extend(value for value in secrets.token_bytes(padding_length - len(padding)) if value)
    encoded = b"\x00\x02" + padding + b"\x00" + message
    encrypted = pow(int.from_bytes(encoded, "big"), identity.exponent, identity.modulus)
    return base64.b64encode(encrypted.to_bytes(width, "big")).decode("ascii")


def signature(identity, nonce, timestamp, token, payload):
    parts = ["Appid=" + identity.app_id, "Nonce=" + nonce, "Time=" + timestamp]
    if token:
        parts.append("Token=" + token)
    if payload:
        parts.append(payload)
    parts.append(identity.app_key)
    return hashlib.md5("&".join(parts).encode("utf-8")).hexdigest()


class CloudClient:
    routes = ALLOWED

    def __init__(self, identity, area, district, opener=None):
        if area not in HOSTS or not re.fullmatch(r"[A-Z]{2}", district):
            raise CloudAPIError("Choose an explicit Cloud API area and two-letter district.")
        self.identity = identity
        self.area, self.district = area, district
        self.opener = opener or build_opener(NoRedirects())
        self.token, self.user_id = "", ""

    def _call(self, method, path, fields):
        if (method, path) not in self.routes:
            raise CloudAPIError("This discovery client only permits authentication and read-only queries.")
        if path != LOGIN and not self.token:
            raise CloudAPIError("Sign in to the Cloud API before querying devices.")
        if method == "POST":
            payload = json.dumps(fields, separators=(",", ":"), ensure_ascii=True)
            data, query = payload.encode("utf-8"), ""
        else:
            fields = dict(sorted(fields.items()))
            payload = "&".join(str(key) + "=" + str(value) for key, value in fields.items())
            data, query = None, "?" + urlencode(fields)
        nonce, timestamp = secrets.token_hex(16).upper(), str(int(time.time() * 1000))
        headers = {"lang": "en", "app-version": self.identity.version,
                   "phone-model": "Android", "sys-type": "1", "sys-version": "14",
                   "content-type": "application/json; charset=utf-8", "appid": self.identity.app_id,
                   "token": self.token, "userid": self.user_id, "nonce": nonce, "time": timestamp,
                   "area": self.area, "sign": signature(self.identity, nonce, timestamp, self.token, payload)}
        request = Request(HOSTS[self.area] + PREFIX + path + query, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise CloudAPIError("The Cloud API returned an oversized response.")
            result = json.loads(raw)
        except HTTPError as exc:
            exc.close()
            raise CloudAPIError("The Cloud API rejected the request (HTTP %s)." % exc.code) from None
        except (OSError, URLError, ValueError):
            raise CloudAPIError("The Cloud API could not be reached or returned an unreadable response.") from None
        if not isinstance(result, dict):
            raise CloudAPIError("The Cloud API returned an unexpected response shape.")
        if result.get("code") != 0:
            value = result.get("code")
            code = value if type(value) is int else None
            raise CloudAPIError("The Cloud API rejected the request" + (" (code %d)." % code if code is not None else "."), code)
        return result.get("result", result.get("data"))

    def login(self, username, password):
        self.token, self.user_id = "", ""
        result = self._call("POST", LOGIN, {"account": username, "district": self.district,
                            "encryptType": 2, "guardCode": "", "password": encrypt_password(password, self.identity)})
        if not isinstance(result, dict) or not isinstance(result.get("token"), str) or not result["token"]:
            raise CloudAPIError("Cloud sign-in did not return a session.")
        user_info = result.get("userInfo")
        user = result.get("userId") or (user_info.get("userId") if isinstance(user_info, dict) else None)
        if not isinstance(user, (str, int)) or isinstance(user, bool) or not user:
            raise CloudAPIError("Cloud sign-in did not return an account identifier.")
        self.token, self.user_id = result["token"], str(user)

    def homes(self, start=0):
        return self._call("GET", HOMES, {"needDefaultRoom": "false", "size": 300, "startIndex": start})

    def devices(self, home_id, start=0):
        return self._call("GET", DEVICES, {"positionId": home_id, "size": 300, "startIndex": start})

    def details(self, device_id):
        return self._call("GET", DETAIL, {"area": self.area, "dids": json.dumps([device_id], separators=(",", ":"))})

    def status(self, device_id):
        return self._call("POST", RESOURCES, {"data": [{"subjectId": device_id,
                            "options": ["batt_0_remain_percentage", "lock_state", "device_offline_status"]}]})

    def plugins(self, model):
        return self._call("GET", PLUGINS, {"model": model})

    def matter_details(self, device_id):
        return self._call("GET", MATTER_DETAIL, {"did": device_id, "fullValue": "true"})

    def matter_status(self, device_id, resources):
        return self._call("POST", MATTER_RESOURCES, {"data": [{"subjectId": device_id,
                            "options": list(resources)}]})

    def panels(self, device_id):
        """Read endpoint paths for Zigbee and hub devices without assuming Matter."""
        return self._call("GET", PANELS, {"subjectIds": device_id, "types": "device_endpoint_panel"})

    def specification(self):
        """Global semantics are reference data, never proof of device support."""
        return self._call("GET", SPEC, {})

    def definitions(self):
        """Download the app's server-maintained endpoint definitions as data.

        The API supplies a gzip URL and MD5. The CDN request receives no app
        identity, account token, or cookies, and cannot follow redirects.
        """
        config = self._call("GET", DEFINITION_URL, {})
        if not isinstance(config, dict) or not isinstance(config.get("url"), str):
            raise CloudAPIError("The cloud endpoint definitions are unavailable.")
        url = urlsplit(config["url"])
        host = url.hostname or ""
        allowed = host.endswith((".aqara.com", ".aqara.cn")) or (
            host.startswith("aiot-") and host.endswith((".amazonaws.com", ".myqcloud.com")))
        if (url.scheme != "https" or not allowed or url.username or url.password or url.port not in (None, 443)
                or not re.fullmatch(r"[a-fA-F0-9]{32}", str(config.get("md5", "")))):
            raise CloudAPIError("The definition download address or checksum is invalid.")
        try:
            with build_opener(NoRedirects()).open(Request(config["url"]), timeout=20) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024 or hashlib.md5(raw).hexdigest().lower() != config["md5"].lower():
                raise CloudAPIError("The definition download failed its size or checksum check.")
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                data = stream.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                raise CloudAPIError("The endpoint definitions exceed the supported size.")
            value = json.loads(data)
            if not isinstance(value, dict) or not isinstance(value.get("configs"), list):
                raise CloudAPIError("The endpoint definitions have an unexpected shape.")
            return value
        except (OSError, URLError, ValueError, EOFError):
            raise CloudAPIError("The cloud endpoint definitions could not be downloaded or decoded.") from None

    def gateway_details(self, device_id):
        return self._call("GET", GATEWAY_DETAIL, {"deviceId": device_id})

    def bridge_gateways(self, device_id, home_id):
        """Read bridge eligibility; this does not bind or reconfigure a device."""
        return self._call("POST", BRIDGE_GATEWAYS, {"deviceId": device_id, "positionId": home_id})

    def bridge_devices(self, home_id, gateway_id):
        return self._call("POST", BRIDGE_DEVICES, {"positionId": home_id, "gatewayId": gateway_id})

    def traits(self, device_id, paths):
        paths = list(paths)
        if not paths or len(paths) > 300 or any(not isinstance(path, str) or not re.fullmatch(r"\d+\.\d+\.\d+", path) for path in paths):
            raise CloudAPIError("Choose explicit three-part trait paths from device metadata.")
        return self._call("POST", TRAIT_READ, {"devices": [{"deviceId": device_id,
            "traits": [{"path": path, "needSubscribe": False} for path in paths]}], "needParam": True})
