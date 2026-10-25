import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

import command
from common import AqaraError, Store, controls, normalize
from filter import build_items
from mobile_backend import (BackendClient, MATTER_WRITE, MobileStore, capabilities,
                            readings)
from mobile_transport import AppIdentity, MobileAPIError
from updater import Updater
from workflow import update


def detail(did="example-device", ep=7, commands=(35010, 35011)):
    return {"devices": [{"did": did, "eps": [{"id": ep, "funcs": [{"id": 148,
        "cmds": [{"id": c} for c in commands], "traits": [{"id": 32970}, {"id": 32971}]}]}]}]}


def lock():
    cap = capabilities(detail(), "example-device")[0]
    return {"id": "mobile:example-device#7", "did": "example-device", "endpoint": "7",
            "name": "Entry Lock", "label": "Entry Lock", "type": "DoorLock", "room": "Hall",
            "backend": "mobile", "mobile_commands": cap["commands"], "mobile_readings": cap["readings"]}


class MobileBackendTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.values = {}
        self.keys = {}
        def factory(identifier):
            if identifier not in self.keys:
                key = Mock()
                key.get.side_effect = lambda: self.values.get(identifier)
                key.set.side_effect = lambda value: self.values.update({identifier: value})
                key.delete.side_effect = lambda: self.values.pop(identifier, None)
                self.keys[identifier] = key
            return self.keys[identifier]
        self.factory = factory
        self.store = MobileStore(Path(self.temp.name) / "mobile", keychain_factory=factory)
        self.store.write("settings", {"home_id": "example-home", "home_name": "Example Home",
                                       "homes": [{"id": "example-home", "name": "Example Home", "agent": True,
                                                  "mobile_id": "example-home"}]})
        self.device = lock()
        self.store.write("catalog", {"home_id": "example-home", "devices": [self.device], "scenes": []})
        self.mobile = Mock()
        self.mobile.matter_details.return_value = detail()
        self.mobile.panels.return_value = []
        self.mobile.traits.return_value = []
        self.mobile.model_endpoints.return_value = []
        self.mobile.endpoint_capabilities.side_effect = lambda did, model="": BackendClient.endpoint_capabilities(self.mobile, did, model)
        self.action = {"action": "control", "profile_id": "default", "home_id": "example-home",
                       "id": self.device["id"], "command": "unlock", "value": ""}
        self.updater = Mock()
        self.updater.available.return_value = None

    def test_only_this_devices_advertised_endpoints_enable_commands(self):
        self.assertEqual(controls(self.device).keys(), {"status", "lock", "unlock"})
        self.assertEqual(capabilities(detail("another-device"), "example-device"), [])
        cap = capabilities(detail(ep=12, commands=(35010, 40031)), "example-device")[0]
        self.assertEqual(cap["commands"], {"lock": "12.148.35010.0"})
        self.assertEqual(controls(dict(self.device, mobile_commands={})).keys(), {"status"})
        self.assertEqual(controls({"type": "DoorLock"}).keys(), {"status"})

    def test_lock_ui_has_distinct_titles_completion_and_no_browsing_write(self):
        with patch.object(self.store, "mobile_client") as client:
            items = build_items("Entry Lock", self.store, self.updater)
            self.assertEqual([i["title"] for i in items], ["Show status", "Lock", "Unlock"])
            self.assertEqual(items[-1]["autocomplete"], "Entry Lock unlock")
            self.assertEqual(json.loads(items[-1]["arg"]), self.action)
            client.assert_not_called()

    def test_execution_rechecks_exact_endpoint_and_sends_once_without_fallback(self):
        with patch.object(self.store, "mobile_client", return_value=self.mobile), patch("mobile_backend.dialog", return_value=""):
            message = command.execute(self.action, self.store)
        self.assertEqual(message, "Requesting Entry Lock to unlock.")
        self.mobile.matter_details.assert_called_once_with("example-device")
        self.mobile.write_lock.assert_called_once_with("example-device", "7.148.35011.0", "")

    def test_no_device_password_metadata_skips_the_password_dialog(self):
        self.device["pwd_state"] = 0
        with patch.object(self.store, "mobile_client", return_value=self.mobile), patch("mobile_backend.dialog") as prompt:
            self.store.control(self.device, "unlock")
        prompt.assert_not_called()
        self.mobile.write_lock.assert_called_once_with("example-device", "7.148.35011.0", "")

    def test_changed_capabilities_or_wrong_home_never_write(self):
        self.mobile.matter_details.return_value = detail(ep=8)
        with patch.object(self.store, "mobile_client", return_value=self.mobile), patch("mobile_backend.dialog") as prompt:
            with self.assertRaises(AqaraError):
                command.execute(self.action, self.store)
            self.mobile.write_lock.assert_not_called()
            prompt.assert_not_called()
        with patch.object(self.store, "mobile_client") as client:
            with self.assertRaises(AqaraError):
                command.execute(dict(self.action, home_id="old-home"), self.store)
            client.assert_not_called()

    def test_cancel_rejection_and_timeout_never_retry_or_fallback(self):
        for password, error in [(None, None), ("example-password", MobileAPIError("safe", 1703)),
                                ("", MobileAPIError("timeout"))]:
            self.mobile.reset_mock()
            self.mobile.write_lock.side_effect = error
            with patch.object(self.store, "mobile_client", return_value=self.mobile), patch.object(self.store, "client") as agent, patch("mobile_backend.dialog", return_value=password):
                with self.assertRaises(AqaraError):
                    command.execute(self.action, self.store)
                self.assertEqual(self.mobile.write_lock.call_count, 0 if password is None else 1)
                agent.assert_not_called()

    def test_resource_status_is_a_subtitle_without_raw_identifiers(self):
        self.mobile.matter_status.return_value = [{"subjectId": "example-device", "resourceId": "7.148.32970.1", "value": "1"},
                                                {"resourceId": "7.148.32971.1", "value": "1"}]
        with patch.object(self.store, "mobile_client", return_value=self.mobile):
            result = build_items("Entry Lock status", self.store, self.updater)
            again = build_items("Entry Lock status", self.store, self.updater)
        self.assertEqual(result[0]["title"], "Entry Lock")
        self.assertEqual(result[0]["subtitle"], "Lock: Locked · Door: Closed")
        self.assertEqual(result, again)
        self.mobile.matter_status.assert_called_once()
        self.assertNotIn("example-device", json.dumps(result))

    def test_agent_import_copies_every_catalog_without_modifying_source(self):
        key = Mock()
        key.get.return_value = "example-agent-key"
        source = Store(Path(self.temp.name) / "agent", keychain_factory=lambda identifier: key)
        source.write("settings", {"home_id": "example-home", "home_name": "Example Home", "showstatus": True,
                                  "homes": [{"id": "example-home", "name": "Example Home"}]})
        catalog = {"home_id": "example-home", "devices": normalize([{"endpoint_id": "example-light", "device_name": "Desk Light", "device_type": "DimmableLight"}], "devices"),
                   "scenes": normalize([{"scene_id": "example-scene", "name": "Example Scene One"}], "scenes")}
        source.write("catalog", catalog)
        before = (source.directory / "settings.json").read_bytes()
        empty = MobileStore(Path(self.temp.name) / "import", keychain_factory=self.factory)
        empty.import_agent(source)
        self.assertEqual(empty.read("catalog"), catalog)
        self.assertTrue(empty.read("settings")["showstatus"])
        self.assertEqual((source.directory / "settings.json").read_bytes(), before)
        self.assertEqual(empty.secrets(), {"agent_key": "example-agent-key"})
        with self.assertRaises(AqaraError):
            empty.import_agent(source)
        with patch.object(empty, "status", return_value={"Power": "On"}):
            items = build_items("Desk Light", empty, self.updater)
        self.assertTrue({"Turn on", "Turn off", "Set brightness", "Set color", "Set color temperature"}.issubset({i["title"] for i in items}))
        agent = Mock()
        with patch.object(empty, "client", return_value=agent):
            command.execute({"action": "control", "home_id": "example-home", "profile_id": "default",
                             "id": "example-light", "command": "dim", "value": "50"}, empty)
        agent.request.assert_called_once_with("device/control", {"device_ids": ["example-light"], "attribute": "brightness", "action": "set", "value": "50"})

    def test_hybrid_refresh_preserves_agent_and_keeps_same_name_devices_distinct(self):
        self.values["default"] = json.dumps({"agent_key": "example-key", "token": "example-token"})
        agent = Mock()
        agent.request.side_effect = [[{"positionId": "example-home", "positionName": "Example Home"}],
                                    [{"endpoint_id": "agent-lock", "device_name": "Entry Lock", "device_type": "DoorLock"},
                                     {"endpoint_id": "agent-light", "device_name": "Desk Light", "device_type": "DimmableLight"}],
                                    [{"scene_id": "agent-scene", "scene_name": "Example Scene One"}]]
        self.mobile.catalog.side_effect = [[{"homeId": "example-home", "homeName": "Example Home"}],
                                         [{"did": "example-device", "deviceName": "Entry Lock", "model": "example-model"}],
                                         [{"sceneId": "native-scene", "name": "Example Scene Two"}]]
        with patch.object(self.store, "client", return_value=agent), patch.object(self.store, "mobile_client", return_value=self.mobile):
            self.store.refresh()
        catalog = self.store.read("catalog")
        self.assertEqual({d["id"] for d in catalog["devices"]}, {"agent-lock", "agent-light", "mobile:example-device#7"})
        self.assertEqual(next(d for d in catalog["devices"] if d.get("backend") == "mobile")["name"], "Entry Lock (Mobile)")
        self.assertEqual({s["id"] for s in catalog["scenes"]}, {"agent-scene", "mobile:native-scene"})
        agent_lock = next(d for d in catalog["devices"] if d["id"] == "agent-lock")
        self.assertNotIn("unlock", controls(agent_lock))

    def test_multiple_accounts_and_home_rollback_use_same_mobile_store(self):
        target = self.store.add_profile("Second region", "{}")
        self.assertIsInstance(target, MobileStore)
        self.assertNotEqual(target.profile_id, self.store.profile_id)
        self.assertIsNot(target.keychain, self.store.keychain)
        settings = self.store.read("settings")
        settings["homes"].append({"id": "second-home", "name": "Second Home"})
        self.store.write("settings", settings)
        original = self.store.read("catalog")
        with patch.object(self.store, "refresh", side_effect=MobileAPIError("unavailable")):
            with self.assertRaises(AqaraError):
                self.store.switch_home("second-home")
        self.assertEqual(self.store.read("catalog"), original)
        self.assertEqual(self.store.read("settings")["home_id"], "example-home")

    def test_panel_subject_key_merges_exact_endpoint_without_losing_agent_controls(self):
        self.values["default"] = json.dumps({"agent_key": "example-key", "token": "example-token"})
        agent = Mock()
        agent.request.side_effect = [[{"positionId": "example-home", "positionName": "Example Home"}],
                                    [{"endpoint_id": "example-subject", "device_name": "Desk Light", "device_type": "DimmableLight"}], []]
        self.mobile.catalog.side_effect = [[{"homeId": "example-home", "homeName": "Example Home"}],
                                          [{"did": "example-native-device", "deviceName": "Desk Light"}], []]
        self.mobile.matter_details.side_effect = MobileAPIError("not Matter", 7167)
        self.mobile.panels.return_value = [{"subjects": [{"deviceId": "example-native-device", "endpointId": 2,
             "subjectKey": "example-subject", "paths": ["2.132.32920"]}]}]
        self.mobile.traits.return_value = [{"deviceId": "example-native-device", "traits": [{"path": "2.132.32920", "value": "1"}]}]
        with patch.object(self.store, "client", return_value=agent), patch.object(self.store, "mobile_client", return_value=self.mobile):
            self.store.refresh()
        devices = self.store.read("catalog")["devices"]
        self.assertEqual(len(devices), 1)
        device = devices[0]
        self.assertEqual(device["id"], "example-subject")
        self.assertEqual(device["did"], "example-native-device")
        self.assertEqual(set(controls(device)), {"status", "on", "off", "dim", "color", "ct"})
        with patch.object(self.store, "mobile_client", return_value=self.mobile):
            items = build_items("Desk Light status", self.store, self.updater)
        self.assertEqual(items[0]["subtitle"], "Power: On")

    def test_home_ids_are_linked_only_by_explicit_selection_and_can_be_unlinked(self):
        self.values["default"] = json.dumps({"agent_key": "example-key", "token": "example-token"})
        agent = Mock()
        agent.request.return_value = [{"home_id": "example-home", "home_name": "Example Home"}]
        self.mobile.catalog.return_value = [{"homeId": "native-home", "homeName": "Example Home"}]
        with patch.object(self.store, "client", return_value=agent), patch.object(self.store, "mobile_client", return_value=self.mobile):
            self.assertEqual(len(self.store.discover_homes()), 2)
            items = build_items("home link", self.store, self.updater)
            action = json.loads(items[0]["arg"])
            self.assertEqual(action["mobile_home_id"], "native-home")
            command.execute(action, self.store)
            homes = self.store.discover_homes()
            self.assertEqual(len(homes), 1)
            self.assertEqual(homes[0]["id"], "example-home")
            self.assertEqual(homes[0]["mobile_id"], "native-home")
            self.store.link_home("example-home", None)
            self.assertEqual(len(self.store.discover_homes()), 2)
            with self.assertRaises(AqaraError):
                self.store.link_home("example-home", "unavailable-home")
            with self.assertRaises(AqaraError):
                command.execute(dict(action, home_id="stale-home"), self.store)

    def test_native_command_rechecks_model_home_and_home_link_before_writing(self):
        self.device["model"] = "example.lock"
        self.mobile.details.return_value = [{"did": "example-device", "model": "example.lock", "homeId": "example-home"}]
        with patch.object(self.store, "mobile_client", return_value=self.mobile), patch("mobile_backend.dialog", return_value=""):
            self.store.control(self.device, "lock")
        self.mobile.write_lock.assert_called_once_with("example-device", "7.148.35010.0", "")
        self.mobile.reset_mock()
        self.mobile.details.return_value[0]["homeId"] = "other-home"
        with patch.object(self.store, "mobile_client", return_value=self.mobile), self.assertRaises(AqaraError):
            self.store.control(self.device, "lock")
        self.mobile.write_lock.assert_not_called()
        self.mobile.details.return_value[0]["homeId"] = "example-home"
        def relink(*args, **kwargs):
            settings = self.store.read("settings")
            settings["home_links"] = {"example-home": "other-home"}
            self.store.write("settings", settings)
            return ""
        with patch.object(self.store, "mobile_client", return_value=self.mobile), patch("mobile_backend.dialog", side_effect=relink), self.assertRaises(AqaraError):
            self.store.control(self.device, "unlock")
        self.mobile.write_lock.assert_not_called()

    def test_reconnection_failure_keeps_credentials_and_current_catalog(self):
        self.values["default"] = json.dumps({"agent_key": "old-example-key"})
        old = self.store.read("catalog")
        with patch.object(self.store, "discover_homes", side_effect=MobileAPIError("unavailable")):
            with self.assertRaises(AqaraError):
                self.store.replace_connection({"agent_key": "new-example-key"})
        self.assertEqual(self.store.secrets(), {"agent_key": "old-example-key"})
        self.assertEqual(self.store.read("catalog"), old)

    def test_home_changed_during_capability_read_never_writes(self):
        def switch(did):
            self.store.write("settings", {"home_id": "other-home"})
            return detail()
        self.mobile.matter_details.side_effect = switch
        with patch.object(self.store, "mobile_client", return_value=self.mobile), patch("mobile_backend.dialog", return_value=""):
            with self.assertRaises(AqaraError):
                command.execute(self.action, self.store)
        self.mobile.write_lock.assert_not_called()

    def test_home_changed_while_entering_password_never_writes(self):
        def switch(*args, **kwargs):
            self.store.write("settings", {"home_id": "other-home"})
            return ""
        with patch.object(self.store, "mobile_client", return_value=self.mobile), patch("mobile_backend.dialog", side_effect=switch):
            with self.assertRaises(AqaraError):
                command.execute(self.action, self.store)
        self.mobile.write_lock.assert_not_called()

    def test_native_scene_uses_exact_app_request_without_retry(self):
        self.mobile.user_id = "example-user"
        with patch.object(self.store, "mobile_client", return_value=self.mobile):
            self.store.run_scene({"backend": "mobile", "scene_id": "example-scene"})
        self.mobile._call.assert_called_once_with("POST", "/ifttt/scene/run", {"sceneId": "example-scene", "userId": "example-user"})


class MobileTransportAndPackageTest(unittest.TestCase):
    def client(self):
        identity = AppIdentity("exampleappid0000", "example_application_key", (1 << 2048) - 159)
        opener = Mock()
        response = io.BytesIO(b'{"code":0,"result":{}}')
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        client = BackendClient(identity, "US", "US", opener)
        client.token, client.user_id = "example-token", "example-user"
        return client, opener

    def test_lock_transport_encodes_device_password_and_validates_route(self):
        client, opener = self.client()
        client.write_lock("example-device", "7.148.35011.0", "example-device-password")
        request = opener.open.call_args[0][0]
        self.assertEqual(json.loads(request.data), {"did": "example-device", "pwd": hashlib.md5(b"example-device-password").hexdigest(),
                                                   "data": {"7.148.35011.0": ""}, "type": 0})
        self.assertNotIn(b"example-device-password", request.data)
        self.assertEqual(request.method, "POST")
        self.assertTrue(request.full_url.endswith(MATTER_WRITE))
        for path in ("7.148.40031.0", "7.148.35011.1", "arbitrary"):
            with self.assertRaises(AqaraError):
                client.write_lock("example-device", path)
        self.assertEqual(opener.open.call_count, 1)

    def test_pagination_supports_native_home_keys_and_refuses_repeated_or_short_pages(self):
        client, _ = self.client()
        page = [{"homeId": "example-home-" + str(i), "homeName": "Home"} for i in range(300)]
        with patch.object(client, "homes", side_effect=[{"homes": page, "count": 301}, {"homes": [{"homeId": "last-home"}], "count": 301}]):
            self.assertEqual(len(client.catalog("homes")), 301)
        for responses in [[{"homes": page, "count": 600}, {"homes": page, "count": 600}],
                          [{"homes": [{"homeId": "example-home"}], "count": 2}]]:
            with patch.object(client, "homes", side_effect=responses), self.assertRaises(AqaraError):
                client.catalog("homes")

    def test_scene_pagination_uses_scene_id_instead_of_shared_position(self):
        client, _ = self.client()
        page = {"scenes": [{"sceneId": "example-scene-one", "positionId": "same-home"},
                           {"sceneId": "example-scene-two", "positionId": "same-home"}], "count": 2}
        with patch.object(client, "_call", return_value=page):
            self.assertEqual(len(client.catalog("scenes", "same-home")), 2)

    def test_named_status_does_not_guess_matter_enums_or_expand_json(self):
        self.assertEqual(readings([{"attr": "lock_state", "value": "1"}]), {"Lock": "Reported state 1"})
        self.assertEqual(readings({"batt_0_remain_percentage": "80"}), {"Battery": "80%"})
        self.assertEqual(readings([{"attr": "lock_state", "value": '{"private-field":"private-value"}'}]),
                         {"Status": "No readable status values returned"})

    def test_mobile_package_identity_allowlist_and_stable_source_are_isolated(self):
        root = Path(__file__).resolve().parents[1]
        original = (root / "info.plist").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "mobile.alfredworkflow"
            subprocess.run([sys.executable, "workflow-build", "--mobile", str(output)], cwd=root, check=True, capture_output=True)
            with ZipFile(output) as archive:
                info = plistlib.loads(archive.read("info.plist"))
                self.assertEqual(info["bundleid"], "com.schwark.aqara.mobile")
                self.assertEqual(info["variables"]["keyword"], "aqm")
                self.assertEqual(info["variables"]["AQARA_BACKEND"], "mobile")
                self.assertEqual(info["version"], archive.read("version").decode().strip())
                self.assertIn("mobile_backend.py", archive.namelist())
                self.assertIn("runtime.py", archive.namelist())
                self.assertFalse(any(name.startswith(("tools/", ".cache/", "tests/")) or name.endswith(".json") for name in archive.namelist()))
                self.assertIn("cloud_capabilities.py", archive.namelist())
                self.assertEqual(len(archive.namelist()), 23)
        self.assertEqual((root / "info.plist").read_bytes(), original)

    def test_update_channels_cannot_cross_install_variants(self):
        from workflow.update import Download
        mobile = Download("https://example.invalid", "Aqara-Mobile-0.3.0-mobile.2.alfredworkflow", "0.3.0-mobile.2", True)
        stable = Download("https://example.invalid", "Aqara-5.0.0.alfredworkflow", "5.0.0")
        with patch("updater.BUNDLE_ID", "com.schwark.aqara.mobile"):
            self.assertIs(update.latest_download([stable, mobile], "5.8", prereleases=True), mobile)
        with patch("updater.BUNDLE_ID", "com.schwark.aqara"):
            self.assertIs(update.latest_download([stable, mobile], "5.8", prereleases=True), stable)


if __name__ == "__main__":
    unittest.main()
