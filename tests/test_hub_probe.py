import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from tools.hub_probe import ProbeError, inspect_hub, parse_advertisement, save_discovery


class HubProbeTest(unittest.TestCase):
    def setUp(self):
        self.config = {"host": "192.168.50.20", "port": 12345, "did": "lumi1.example-hub"}
        self.transport = Mock()
        self.transport.connect = AsyncMock()
        self.transport.send = AsyncMock()
        self.transport.close = AsyncMock()
        self.transport.receive = AsyncMock()
        self.factory = Mock(return_value=self.transport)

    def test_handshake_only_never_sends_application_messages(self):
        result = asyncio.run(inspect_hub(self.config, self.factory))
        self.assertTrue(result["handshake"])
        self.assertFalse(result["authenticated"])
        self.transport.send.assert_not_called()
        self.transport.close.assert_awaited_once()

    def test_checkin_observes_topology_and_never_sends_device_controls(self):
        self.transport.receive.side_effect = [{"cmd": "topology", "data": {}},
                                             {"cmd": "checkin_done", "seq": 1, "data": {"code": 0}}]
        result = asyncio.run(inspect_hub(self.config, self.factory, {"token": "example-token", "user_id": "example-user"}))
        self.assertTrue(result["authenticated"])
        self.assertTrue(result["topology_observed"])
        messages = [call.args[0] for call in self.transport.send.call_args_list]
        self.assertEqual([m["cmd"] for m in messages], ["checkin"])
        self.assertNotIn("example-token", str(result))
        self.transport.close.assert_awaited_once()

    def test_rejection_does_not_retry_and_closes(self):
        self.transport.receive.return_value = {"cmd": "checkin_done", "seq": 1, "data": {"code": 7, "message": "private-server-error"}}
        with self.assertRaises(ProbeError) as caught:
            asyncio.run(inspect_hub(self.config, self.factory, {"token": "example-token", "user_id": "example-user"}))
        self.assertNotIn("private-server-error", str(caught.exception))
        self.assertEqual(self.transport.send.call_count, 1)
        self.transport.close.assert_awaited_once()

    def test_public_destination_and_missing_session_never_connect(self):
        for config, credentials in [(dict(self.config, host="8.8.8.8"), None), (self.config, {})]:
            with self.assertRaises(ProbeError):
                asyncio.run(inspect_hub(config, self.factory, credentials))
        self.transport.connect.assert_not_called()

    def test_advertisements_require_native_lan_capability_flags(self):
        sample = ("Example._aqara-setup._tcp.local. can be reached at example-m3.local.:12345 (interface 4)\n"
                  "id=lumi1.example-hub pv=true ac=2 ca=32 md=example.gateway")
        result = parse_advertisement(sample)
        self.assertEqual(result, dict(self.config, host="example-m3.local.", model="example.gateway"))
        for invalid in (sample.replace("ca=32", "ca=0"), sample.replace("pv=true", "pv=false"), sample.replace("ac=2", "ac=1"),
                        sample.replace("example-m3.local.", "example.invalid.")):
            self.assertIsNone(parse_advertisement(invalid))

    def test_discovery_file_is_private_and_never_in_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "private" / "hubs.json"
            save_discovery(destination, [self.config])
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ProbeError):
            save_discovery(Path(__file__).resolve().parents[1] / "forbidden-hubs.json", [])


if __name__ == "__main__":
    unittest.main()
