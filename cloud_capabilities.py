"""Common Aqara trait semantics, bound to returned device endpoints.

These are protocol functions, not a catalogue of device models. Unknown traits
remain read-only; a global definition alone never enables a device command.
"""

import math
import re

from common import AqaraError


PATH = re.compile(r"\d+\.\d+\.\d+")
SEMANTICS = {"132.32920": "Power", "133.32923": "Brightness",
             "134.32927": "Color temperature", "148.32970": "Lock",
             "148.32971": "Door"}


def objects(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def panel_endpoints(payload, did):
    """Keep only this device, exact endpoint paths, and unambiguous subjects."""
    result, seen = [], set()
    for panel in objects(payload):
        for row in objects(panel.get("subjects")):
            ep = str(row.get("endpointId", ""))
            if str(row.get("deviceId", "")) != did or not ep.isdecimal():
                continue
            if ep in seen:
                raise AqaraError("The cloud returned duplicate endpoints. Previous data was kept.")
            seen.add(ep)
            advertised = row.get("paths")
            if not isinstance(advertised, list):
                raise AqaraError("The cloud returned an invalid endpoint path list. Previous data was kept.")
            paths = sorted({p for p in advertised if isinstance(p, str)
                            and PATH.fullmatch(p) and p.split(".")[0] == ep})
            result.append({"endpoint": ep, "paths": paths,
                           "subject_key": row.get("subjectKey") if isinstance(row.get("subjectKey"), str) else "",
                           "name": str(row.get("endpointName") or ""),
                           "type": str(row.get("deviceTypes") or "")})
    return result


def trait_rows(payload, did, requested):
    """Ignore unsolicited subjects/paths; reject ambiguous duplicate readings."""
    result = {}
    requested = set(requested)
    for device in objects(payload):
        if str(device.get("deviceId", "")) != did:
            continue
        for row in objects(device.get("traits")):
            path = row.get("path")
            if isinstance(path, str) and path in requested:
                if path in result:
                    raise AqaraError("The cloud returned duplicate traits. Previous data was kept.")
                result[path] = row
    return result


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def bounds(row, low, high):
    """Device bounds may narrow known protocol bounds, never widen them."""
    minimum = number(row.get("minValue", row.get("min")))
    maximum = number(row.get("maxValue", row.get("max")))
    return max(low, minimum if minimum is not None else low), min(high, maximum if maximum is not None else high)


def compile_traits(endpoint, traits):
    controls, readings = {}, {}
    ep = endpoint["endpoint"]
    for path in endpoint.get("paths", []):
        if path not in traits or path.split(".")[0] != ep:
            continue
        row, semantic = traits[path], ".".join(path.split(".")[1:])
        if semantic in SEMANTICS:
            readings[path] = SEMANTICS[semantic]
        if semantic == "132.32920":
            controls.update(on={"path": path, "kind": "power", "value": "1"},
                            off={"path": path, "kind": "power", "value": "0"})
        elif semantic == "133.32923":
            low, high = bounds(row, 1, 100)
            if math.ceil(low) <= math.floor(high):
                controls["dim"] = {"path": path, "kind": "brightness", "low": math.ceil(low), "high": math.floor(high)}
        elif semantic == "134.32927":
            # Aqara's ColorTemperature trait is in mireds, not Kelvin.
            # Require the device's actual range before offering a set action.
            low = number(row.get("minValue", row.get("min")))
            high = number(row.get("maxValue", row.get("max")))
            if low is not None and high is not None and 1 <= low <= high <= 65534:
                controls["ct"] = {"path": path, "kind": "mired", "mired_low": low, "mired_high": high,
                                  "low": math.ceil(1000000 / high), "high": math.floor(1000000 / low)}
    return controls, readings


def validate_control(spec, value=""):
    if spec.get("kind") == "power":
        if value:
            raise AqaraError("This command does not accept a value.")
        if spec.get("value") not in ("0", "1"):
            raise AqaraError("The native power value is invalid.")
        return spec["value"]
    if spec.get("kind") not in ("brightness", "mired"):
        raise AqaraError("This native control is not implemented.")
    low, high = spec["low"], spec["high"]
    try:
        amount = int(value)
    except (ValueError, TypeError):
        raise AqaraError("Enter a whole number from %s to %s." % (low, high)) from None
    if not low <= amount <= high:
        raise AqaraError("Enter a value from %s to %s." % (low, high))
    if spec["kind"] == "mired":
        # Round the unit conversion, then enforce the advertised wire bounds.
        amount = round(1000000 / amount)
        if not spec["mired_low"] <= amount <= spec["mired_high"]:
            raise AqaraError("The color temperature is outside this device's range.")
    return str(amount)


def control_hint(spec):
    return ("%s–%s%s" % (spec["low"], spec["high"], " K" if spec["kind"] == "mired" else "")) if "low" in spec else None


def trait_status(payload, did, paths):
    result = {}
    for path, row in trait_rows(payload, did, paths).items():
        value = row.get("value")
        label = paths[path]
        if label == "Power":
            text = {"0": "Off", "1": "On", "false": "Off", "true": "On"}.get(str(value).casefold())
        elif label in ("Lock", "Door"):
            values = {"Lock": {"0": "Not fully locked", "1": "Locked", "2": "Unlocked"},
                      "Door": {"0": "Open", "1": "Closed", "2": "Jammed", "3": "Forced open", "4": "Error", "5": "Ajar"}}
            text = values[label].get(str(value))
        else:
            amount = number(value)
            text = ("%s%%" % format(amount, "g") if label == "Brightness" and 0 <= amount <= 100 else
                    "%s K" % round(1000000 / amount) if label == "Color temperature" and amount > 0 else None) if amount is not None else None
        if text:
            result[label] = text
    return result or {"Status": "No readable status values returned"}
