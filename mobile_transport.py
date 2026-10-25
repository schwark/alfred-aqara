"""Shared signed transport for the experimental Aqara mobile backend.

Wire-format reference: home-assistant-plugins/aquara-local at
4fc274688cd40774307549172c9ae1db073c92ca (MIT). App constants are loaded from
that separately obtained reference, never embedded here. Credentials and
sessions stay in memory. Model-specific lock control remains unverified.
"""

import ast
import base64
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
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
ALLOWED = {("POST", LOGIN), ("GET", HOMES), ("GET", DEVICES),
           ("GET", DETAIL), ("POST", RESOURCES), ("GET", PLUGINS),
           ("GET", MATTER_DETAIL), ("POST", MATTER_RESOURCES),
           ("POST", TRAIT_READ), ("GET", PANELS), ("GET", SPEC)}
MAX_RESPONSE = 4 * 1024 * 1024


class MobileAPIError(AqaraError):
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

    @classmethod
    def from_reference(cls, filename):
        """Read only literal constants; imports and other Python are never executed."""
        try:
            values = {}
            for node in ast.parse(Path(filename).read_text()).body:
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
            identity = cls(values["APPID"], values["APPKEY"], values["RSA_N"], values["RSA_E"])
            if (not isinstance(identity.app_id, str) or not re.fullmatch(r"[a-zA-Z0-9]{8,64}", identity.app_id)
                    or not isinstance(identity.app_key, str) or not 16 <= len(identity.app_key) <= 128
                    or type(identity.modulus) is not int or not 1024 <= identity.modulus.bit_length() <= 8192
                    or identity.exponent != 65537):
                raise ValueError()
            return identity
        except (OSError, ValueError, TypeError, KeyError, SyntaxError):
            raise MobileAPIError("The mobile app reference constants are missing or invalid.") from None


def encrypt_password(password, identity):
    message = hashlib.md5(password.encode("utf-8")).hexdigest().encode("ascii")
    width = (identity.modulus.bit_length() + 7) // 8
    padding_length = width - len(message) - 3
    if padding_length < 8:
        raise MobileAPIError("Invalid mobile app encryption key.")
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


class MobileClient:
    routes = ALLOWED

    def __init__(self, identity, area, district, opener=None):
        if area not in HOSTS or not re.fullmatch(r"[A-Z]{2}", district):
            raise MobileAPIError("Choose an explicit mobile API area and two-letter district.")
        self.identity = identity
        self.area, self.district = area, district
        self.opener = opener or build_opener(NoRedirects())
        self.token, self.user_id = "", ""

    def _call(self, method, path, fields):
        if (method, path) not in self.routes:
            raise MobileAPIError("This discovery client only permits authentication and read-only queries.")
        if path != LOGIN and not self.token:
            raise MobileAPIError("Sign in to the mobile API before querying devices.")
        if method == "POST":
            payload = json.dumps(fields, separators=(",", ":"), ensure_ascii=True)
            data, query = payload.encode("utf-8"), ""
        else:
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
                raise MobileAPIError("The mobile API returned an oversized response.")
            result = json.loads(raw)
        except HTTPError as exc:
            exc.close()
            raise MobileAPIError("The mobile API rejected the request (HTTP %s)." % exc.code) from None
        except (OSError, URLError, ValueError):
            raise MobileAPIError("The mobile API could not be reached or returned an unreadable response.") from None
        if not isinstance(result, dict):
            raise MobileAPIError("The mobile API returned an unexpected response shape.")
        if result.get("code") != 0:
            value = result.get("code")
            code = value if type(value) is int else None
            raise MobileAPIError("The mobile API rejected the request" + (" (code %d)." % code if code is not None else "."), code)
        return result.get("result", result.get("data"))

    def login(self, username, password):
        self.token, self.user_id = "", ""
        result = self._call("POST", LOGIN, {"account": username, "district": self.district,
                            "encryptType": 2, "guardCode": "", "password": encrypt_password(password, self.identity)})
        if not isinstance(result, dict) or not isinstance(result.get("token"), str) or not result["token"]:
            raise MobileAPIError("Mobile sign-in did not return a session.")
        user_info = result.get("userInfo")
        user = result.get("userId") or (user_info.get("userId") if isinstance(user_info, dict) else None)
        if not isinstance(user, (str, int)) or isinstance(user, bool) or not user:
            raise MobileAPIError("Mobile sign-in did not return an account identifier.")
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

    def traits(self, device_id, paths):
        paths = list(paths)
        if not paths or len(paths) > 300 or any(not isinstance(path, str) or not re.fullmatch(r"\d+\.\d+\.\d+", path) for path in paths):
            raise MobileAPIError("Choose explicit three-part trait paths from device metadata.")
        return self._call("POST", TRAIT_READ, {"devices": [{"deviceId": device_id,
            "traits": [{"path": path, "needSubscribe": False} for path in paths]}], "needParam": True})
