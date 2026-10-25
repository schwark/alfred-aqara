"""Read-only M3 LANLink discovery and handshake research (not packaged).

Uses three separately obtained MIT-licensed protocol modules, pinned by SHA256.
No device writes, scene execution, pairing, settings changes, or lock operations.
Optional check-in uses an existing mobile session from Keychain, never argv.
"""

import argparse
import asyncio
import hashlib
import importlib.util
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import types


SERVICE = "_aqara-setup._tcp."
REFERENCE_COMMIT = "7ddd5314019d8fde8d29d090d93165f3196c7df7"
REFERENCE_HASHES = {
    "crypto.py": "bd7a56c5d7ce45b096fb22429b69a6ef5a720f8ca2e60b39ae88560c0c8f0969",
    "ecc.py": "aed88c93890a638861ed29f9c2bb951b2ed057abfa3b4cf5e997f38739de1add",
    "tunnel.py": "f16c55fd0485f065bde4302c97a579ea8910ad5bf22f6cde570cb25fe74d8374",
}


class ProbeError(Exception):
    """Diagnostics never include host addresses, device IDs, or credentials."""


def dns_sd(arguments, timeout):
    process = subprocess.Popen(["/usr/bin/dns-sd"] + arguments,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.terminate()
        output, _ = process.communicate(timeout=2)
    return output.decode("utf-8", errors="replace")


def parse_advertisement(text):
    address = re.search(r"can be reached at ([A-Za-z0-9_.%-]+):(\d+)", text)
    fields = dict(re.findall(r"(?:^|\s)([A-Za-z]+)=([^\s]+)", text))
    try:
        capable = fields.get("pv") == "true" and fields.get("ac") == "2" and bool(int(fields.get("ca", "0")) & 32)
        port = int(address.group(2)) if address else 0
        did = fields.get("id", "")
        if (not capable or not address or not 1 <= port <= 65535
                or not re.fullmatch(r"lumi[\w.-]+", did) or not address.group(1).endswith(".local.")):
            return None
        return {"host": address.group(1), "port": port, "did": did, "model": fields.get("md", "")}
    except ValueError:
        return None


def discover(timeout=6):
    output = dns_sd(["-B", SERVICE, "local."], timeout)
    names = {line.split(SERVICE, 1)[1].strip() for line in output.splitlines()
             if SERVICE in line and re.search(r"\sAdd\s", line)}
    result = []
    for name in sorted(names)[:16]:
        record = parse_advertisement(dns_sd(["-L", name, SERVICE, "local."], 3))
        if record and record["did"] not in {item["did"] for item in result}:
            result.append(record)
    return result


def load_tunnel(reference):
    directory = Path(reference) / "custom_components/aqara_lanlink/hub"
    for name, digest in REFERENCE_HASHES.items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
            raise ProbeError("The LANLink reference differs from the reviewed pinned revision.")
    namespace = "_aqara_lan_reference"
    package = types.ModuleType(namespace)
    package.__path__ = [str(directory)]
    sys.modules[namespace] = package
    for filename in REFERENCE_HASHES:
        name = namespace + "." + filename[:-3]
        spec = importlib.util.spec_from_file_location(name, directory / filename)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        # The reference has verbose packet logs. Keep research secrets out of logs.
        logging.getLogger(name).disabled = True
    base = sys.modules[namespace + ".tunnel"].EncryptedTunnel
    class ReviewedM3Tunnel(base):
        @staticmethod
        def _verify_server_proof(key, proof):
            # The reference accepts unknown proof layouts for compatibility.
            # This M3-only probe requires the captured layout before check-in.
            return (len(proof) == 52 and proof[:2] == b"\x00\x10" and proof[18:20] == b"\x00\x20"
                    and base._verify_server_proof(key, proof))
    return ReviewedM3Tunnel


async def inspect_hub(config, tunnel_class, credentials=None, timeout=10):
    """Handshake, optionally authenticate, and observe the hub's topology push."""
    did, host, port = config.get("did"), config.get("host"), config.get("port")
    if (not isinstance(did, str) or not re.fullmatch(r"lumi[\w.-]+", did)
            or not isinstance(host, str) or not host or type(port) is not int or not 1 <= port <= 65535):
        raise ProbeError("Select a valid hub from private discovery data.")
    if not host.lower().rstrip(".").endswith(".local"):
        try:
            if not ipaddress.ip_address(host.split("%", 1)[0]).is_private:
                raise ValueError()
        except ValueError:
            raise ProbeError("Use the hub's local address or a private address reachable through your own network.") from None
    if credentials is not None and not all(isinstance(credentials.get(key), str) and credentials[key]
                                          for key in ("token", "user_id")):
        raise ProbeError("This profile needs a mobile session. Connect it with aqm login.")
    tunnel = tunnel_class(did, keepalive_interval=0)

    async def inspect():
        await tunnel.connect(host, port)
        result = {"handshake": True, "authenticated": False, "topology_observed": False}
        if credentials is None:
            return result
        # This is the only application message this probe can transmit.
        await tunnel.send({"seq": 1, "type": "session", "cmd": "checkin",
                           "data": {"user": credentials["user_id"], "did": did, "aid": credentials["token"]}})
        for _ in range(64):
            message = await tunnel.receive()
            if not isinstance(message, dict):
                raise ProbeError("The hub closed its session before authentication completed.")
            if message.get("cmd") == "topology":
                result["topology_observed"] = True
            if message.get("cmd") == "checkin_done" and message.get("seq") == 1:
                data = message.get("data")
                if not isinstance(data, dict) or type(data.get("code")) is not int or data["code"] != 0:
                    raise ProbeError("The hub rejected this session. No device commands were sent.")
                result["authenticated"] = True
                return result
        raise ProbeError("The hub did not complete check-in within the response limit.")
    try:
        return await asyncio.wait_for(inspect(), timeout)
    finally:
        try:
            await asyncio.wait_for(tunnel.close(), 2)
        except (OSError, asyncio.TimeoutError):
            pass


def save_discovery(path, records):
    path = Path(path).resolve()
    checkout = Path(__file__).resolve().parents[1]
    if path == checkout or checkout in path.parents:
        raise ProbeError("Save private network discovery outside the source checkout.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump({"candidates": records}, stream)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--output", type=Path, help="Private discovery file outside the checkout")
    parser.add_argument("--reference", type=Path, help="Separately cloned Aqara-LANLink reference")
    parser.add_argument("--profile", help="Optional existing mobile profile ID for hub check-in")
    args = parser.parse_args()
    try:
        if args.discover:
            records = discover()
            if args.output:
                save_discovery(args.output, records)
            print(json.dumps({"lanlink_candidates": len(records), "device_commands_sent": False}))
            return
        if not args.reference:
            parser.error("Provide --reference, with the selected hub JSON on stdin, or use --discover.")
        credentials = None
        if args.profile:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from mobile_backend import MobileStore
            credentials = MobileStore(profile_id=args.profile).secrets()
        config = json.loads(sys.stdin.read(16385))
        if not isinstance(config, dict):
            raise ProbeError("Pass the selected private hub object on stdin.")
        result = asyncio.run(inspect_hub(config, load_tunnel(args.reference), credentials))
        print(json.dumps(dict(result, device_commands_sent=False)))
    except ProbeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        # Network/library exceptions can contain host addresses and frame contents.
        print("Hub probe could not complete. Check discovery, network access, reference revision, and session availability.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
