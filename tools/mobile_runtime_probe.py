"""Inspect the Aqara app in an explicitly selected emulator without controls.

Requires Frida and the separately compiled mobile_runtime_agent.js. Outputs
proprietary runtime code to a private directory outside the checkout by
default. No account credentials, device controls, or pairing are used.
"""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import time
import zlib


PACKAGE = "com.lumiunited.aqarahome.play"


def repaired_dex(data):
    """Include relocated methods and recompute header metadata in a copy."""
    if len(data) < 112 or not data.startswith(b"dex\n"):
        raise ValueError("Not a standard DEX image")
    output = bytearray(data)
    data_offset = struct.unpack_from("<I", output, 108)[0]
    if not 112 <= data_offset <= len(output):
        raise ValueError("Invalid DEX data offset")
    struct.pack_into("<I", output, 32, len(output))
    struct.pack_into("<I", output, 104, len(output) - data_offset)
    output[12:32] = hashlib.sha1(output[32:]).digest()
    struct.pack_into("<I", output, 8, zlib.adler32(output[12:]) & 0xffffffff)
    return bytes(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True, help="Explicit emulator ID, such as emulator-5560")
    parser.add_argument("--agent-bundle", type=Path, required=True)
    parser.add_argument("--pid", type=int, help="Attach to an existing app process instead of spawning")
    parser.add_argument("--settle-seconds", type=float, default=8)
    parser.add_argument("--output", type=Path, default=Path.home() / ".cache/alfred-aqara/android/private/runtime-probe")
    args = parser.parse_args()
    if not args.device.startswith("emulator-") or not 0 <= args.settle_seconds <= 45:
        parser.error("Select an emulator and a settle interval between 0 and 45 seconds.")
    import frida

    output = args.output.resolve()
    checkout = Path(__file__).resolve().parents[1]
    if output == checkout or checkout in output.parents:
        parser.error("Runtime artifacts must be written outside the source checkout.")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.chmod(0o700)
    device = frida.get_device(args.device, timeout=10)
    pid = args.pid or device.spawn([PACKAGE])
    session = None
    script = None
    try:
        session = device.attach(pid)
        script = session.create_script(args.agent_bundle.read_text())
        script.load()
        if not args.pid:
            device.resume(pid)
        time.sleep(args.settle_seconds)
        metadata = script.exports_sync.inspectclass("com.lumi.arn.pkgs.ahdoorlock.AHDoorLockModule")
        entries = [entry for entry in script.exports_sync.dexranges() if entry["source"] == "anonymous"]
        manifest = {"package": PACKAGE, "frida": frida.__version__, "class": metadata, "images": []}
        for index, entry in enumerate(entries, 1):
            if not entry["size"] <= entry["extent"] <= 200 * 1024 * 1024:
                raise ValueError("Invalid runtime image extent")
            data = script.exports_sync.dumpdex(entry["address"], entry["extent"])
            filename = "classes-%02d.dex" % index
            (output / filename).write_bytes(repaired_dex(data))
            manifest["images"].append({"file": filename, "declared_size": entry["size"], "captured_size": len(data)})
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
        for path in output.iterdir():
            if path.is_file():
                path.chmod(0o600)
        print("Recovered %d private DEX images; no device commands were invoked." % len(entries))
    finally:
        if script is not None:
            script.unload()
        if session is not None:
            session.detach()
        if not args.pid:
            device.kill(pid)


if __name__ == "__main__":
    main()
