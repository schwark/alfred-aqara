import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from cloud_capabilities import compile_traits, panel_endpoints, trait_rows, trait_status, validate_control
from common import AqaraError, control_body, controls
from mobile_backend import BackendClient, MobileStore, TRAIT_WRITE, capabilities
from mobile_transport import AppIdentity, MobileAPIError
from tools.cloud_probe import inspect_device, inspect_profile, save_evidence


def panel(paths=None, subject="example-endpoint"):
    return [{"subjects": [{"deviceId": "example-device", "endpointId": 2,
                            "endpointName": "Desk Light", "subjectKey": subject,
                            "deviceTypes": "DimmableLight",
                            "paths": paths or ["2.132.32920", "2.133.32923", "2.134.32927"]}]}]


def metadata():
    return [{"deviceId": "example-device", "traits": [
        {"path": "2.132.32920", "value": "1"},
        {"path": "2.133.32923", "value": "40", "min": "5", "max": "90"},
        {"path": "2.134.32927", "value": "250", "minValue": "153", "maxValue": "370"}]}]


class CloudCapabilityTest(unittest.TestCase):
    def test_discovery_binds_subject_endpoint_and_returned_traits(self):
        payload = panel()
        payload[0]["subjects"].append({"deviceId": "another-device", "endpointId": 3, "paths": ["3.132.32920"]})
        payload[0]["subjects"][0]["paths"].extend(["3.132.32920", "2.132.32920.1", "unknown"])
        endpoint = panel_endpoints(payload, "example-device")[0]
        self.assertEqual(endpoint["paths"], ["2.132.32920", "2.133.32923", "2.134.32927"])
        traits = trait_rows(metadata(), "example-device", endpoint["paths"])
        commands, readings = compile_traits(endpoint, traits)
        self.assertEqual(set(commands), {"on", "off", "dim", "ct"})
        self.assertEqual(commands["dim"]["low"], 5)
        self.assertEqual(commands["dim"]["high"], 90)
        self.assertEqual(readings["2.132.32920"], "Power")
        self.assertEqual(compile_traits(endpoint, {})[0], {})
        self.assertEqual(trait_rows(metadata(), "another-device", endpoint["paths"]), {})
        with self.assertRaises(AqaraError):
            panel_endpoints(payload + payload, "example-device")
        with self.assertRaises(AqaraError):
            trait_rows(metadata() + metadata(), "example-device", endpoint["paths"])

    def test_values_ranges_and_units_are_validated_without_guessing(self):
        endpoint = panel_endpoints(panel(), "example-device")[0]
        traits = trait_rows(metadata(), "example-device", endpoint["paths"])
        commands, _ = compile_traits(endpoint, traits)
        self.assertEqual(validate_control(commands["on"]), "1")
        self.assertEqual(validate_control(commands["off"]), "0")
        self.assertEqual(validate_control(commands["dim"], "50"), "50")
        self.assertEqual(validate_control(commands["ct"], "4000"), "250")
        for cmd, value in (("on", "true"), ("dim", "0"), ("dim", "91"), ("dim", "nan"), ("ct", "1000")):
            with self.assertRaises(AqaraError):
                validate_control(commands[cmd], value)
        traits["2.134.32927"] = {"path": "2.134.32927"}
        self.assertNotIn("ct", compile_traits(endpoint, traits)[0])

    def test_status_formats_known_traits_and_ignores_unsolicited_private_data(self):
        paths = {"2.132.32920": "Power", "2.133.32923": "Brightness", "2.134.32927": "Color temperature"}
        payload = metadata() + [{"deviceId": "another-device", "traits": [{"path": "2.132.32920", "value": "private-data"}]}]
        self.assertEqual(trait_status(payload, "example-device", paths),
                         {"Power": "On", "Brightness": "40%", "Color temperature": "4000 K"})
        payload[0]["traits"][0]["value"] = {"sensitive": "private-data"}
        self.assertNotIn("private-data", json.dumps(trait_status(payload, "example-device", paths)))

    def client(self):
        client = BackendClient(AppIdentity("exampleappid0000", "example_application_key", (1 << 2048) - 159), "US", "US")
        client.token, client.user_id = "example-token", "example-user"
        return client

    def test_zigbee_discovery_does_not_depend_on_matter(self):
        client = self.client()
        with patch.object(client, "panels", return_value=panel()), patch.object(client, "traits", return_value=metadata()), patch.object(client, "matter_details", side_effect=MobileAPIError("not supported", 7167)):
            endpoints, failures = client.endpoint_capabilities("example-device")
        self.assertEqual(failures, 1)
        self.assertEqual(set(endpoints[0]["cloud_controls"]), {"on", "off", "dim", "ct"})
        self.assertEqual(endpoints[0]["subject_key"], "example-endpoint")

    def test_authentication_trait_takes_priority_over_plain_lock_commands(self):
        matter = {"devices": [{"did": "example-device", "eps": [{"id": 2, "funcs": [{"id": 148,
            "cmds": [{"id": 35010}, {"id": 35011}], "traits": [{"id": 32970}, {"id": 40000}]}]}]}]}
        self.assertEqual(capabilities(matter, "example-device")[0]["commands"], {})
        matter["devices"][0]["eps"][0]["funcs"][0]["traits"].pop()
        client = self.client()
        with patch.object(client, "panels", return_value=panel(["2.148.32970", "2.148.40000"])), patch.object(client, "traits", return_value=[]), patch.object(client, "matter_details", return_value=matter):
            endpoints, _ = client.endpoint_capabilities("example-device")
        self.assertEqual(endpoints[0]["commands"], {})
        self.assertTrue(endpoints[0]["requires_auth"])

    def test_native_writer_cannot_be_used_for_arbitrary_or_lock_traits(self):
        client = self.client()
        with patch.object(client, "_call", return_value={}) as send:
            client.write_trait("example-device", {"path": "2.132.32920", "kind": "power", "value": "1"})
            send.assert_called_once_with("POST", TRAIT_WRITE, {"deviceId": "example-device", "traits": [{"path": "2.132.32920", "value": "1"}]})
            for spec in ({"path": "2.148.33061", "kind": "power", "value": "1"}, {"path": "2.999.999", "kind": "unknown"}):
                with self.assertRaises(AqaraError):
                    client.write_trait("example-device", spec)
            self.assertEqual(send.call_count, 1)

    def test_hybrid_endpoint_keeps_all_agent_commands_and_selects_route_before_write(self):
        client = self.client()
        with patch.object(client, "panels", return_value=panel()), patch.object(client, "traits", return_value=metadata()), patch.object(client, "matter_details", side_effect=MobileAPIError("not supported")):
            cap = client.endpoint_capabilities("example-device")[0][0]
        device = {"id": "example-endpoint", "did": "example-device", "endpoint": "2", "backend": "mobile", "type": "DimmableLight",
                  "cloud_controls": cap["cloud_controls"], "agent_fallback": {"id": "example-endpoint", "type": "DimmableLight"}}
        self.assertEqual(set(controls(device)), {"status", "on", "off", "dim", "color", "ct"})
        # A value outside native limits can still use the original Agent route,
        # decided before any device operation is issued.
        self.assertEqual(control_body(device, "dim", "100")["attribute"], "brightness")
        self.assertEqual(control_body(device, "dim", "50")["attribute"], "native_trait")
        with tempfile.TemporaryDirectory() as directory:
            store = MobileStore(Path(directory), keychain_factory=lambda identifier: Mock())
            store.write("settings", {"home_id": "example-home"})
            agent, mobile = Mock(), Mock()
            mobile.endpoint_capabilities.return_value = ([cap], 0)
            with patch.object(store, "client", return_value=agent), patch.object(store, "mobile_client", return_value=mobile):
                store.control(device, "color", "red")
                agent.request.assert_called_once()
                mobile.write_trait.assert_not_called()
                mobile.write_trait.side_effect = MobileAPIError("timeout")
                with self.assertRaises(MobileAPIError):
                    store.control(device, "dim", "50")
                mobile.write_trait.assert_called_once()
                self.assertEqual(agent.request.call_count, 1)

    def test_probe_is_read_only_and_keeps_evidence_out_of_the_repository(self):
        client = Mock()
        client.panels.return_value = panel()
        client.matter_details.side_effect = MobileAPIError("private-server-data", 7167)
        client.details.return_value = {"token": "private-token", "did": "example-device"}
        client.traits.return_value = metadata()
        result = inspect_device(client, {"did": "example-device"})
        self.assertEqual(result["matter"], {"error": "query_failed", "code": 7167})
        self.assertEqual({call[0] for call in client.mock_calls}, {"panels", "matter_details", "details", "traits"})
        store = Mock()
        store.secrets.return_value = {"agent_key": "example-agent-key"}
        self.assertEqual(inspect_profile(store)[1], {"status": "sign_in_required"})
        store.mobile_client.assert_not_called()
        with tempfile.TemporaryDirectory() as directory, patch("tools.cloud_probe.PRIVATE_ROOT", Path(directory)):
            destination = Path(directory) / "private/evidence.json"
            save_evidence(destination, result)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("private-token", destination.read_text())
            with self.assertRaises(AqaraError):
                save_evidence(Path(__file__).resolve().parents[1] / "forbidden-evidence.json", result)


if __name__ == "__main__":
    unittest.main()
