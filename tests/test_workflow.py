import io
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from zipfile import ZipFile

import command
import common
from common import AqaraError, Client, Store, control_body, normalize
from filter import build_items


DEVICES = {"data": {"home_devices": [
    {"endpoint_id": "light-1", "device_name": "Desk Light", "device_type": "DimmableLight", "position_name": "Office"},
    {"endpoint_id": "shade-1", "device_name": "Curtain", "device_type": "WindowCovering", "position_name": "Bedroom"},
    {"endpoint_id": "sensor-1", "device_name": "Temperature", "device_type": "TemperatureSensor", "position_name": "Office"},
    {"endpoint_id": "ac-1", "device_name": "AC", "device_type": "AirConditioner", "position_name": "Office"},
]}}
SCENES = {"code": 0, "data": {"home_scenes": [{"scene_id": "scene-1", "scene_name": "Good Night"}]}}
HOMES = {"data": [{"positionId": "home-1", "positionName": "My Home"}]}


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.keys = Mock()
        self.keys.get.return_value = "test-key"
        self.profile_keys = {"default": self.keys}
        def keychain_factory(identifier):
            if identifier not in self.profile_keys:
                keychain = Mock()
                keychain.get.return_value = ""
                keychain.set.side_effect = lambda key: setattr(keychain.get, "return_value", key)
                self.profile_keys[identifier] = keychain
            return self.profile_keys[identifier]
        self.keychain_factory = keychain_factory
        self.store = Store(self.temp.name, keychain_factory=keychain_factory)
        self.store.write("settings", {"home_id": "home-1", "home_name": "My Home"})
        self.devices = normalize(DEVICES, "devices")
        self.store.write("catalog", {"home_id": "home-1", "devices": self.devices,
                                     "scenes": normalize(SCENES, "scenes")})
        self.client = Mock()

    def test_browsing_is_local_and_non_actionable_for_devices(self):
        items = build_items("Office", self.store)
        self.assertEqual(len(items), 3)
        self.assertTrue(all(not item["valid"] for item in items))
        self.keys.get.assert_not_called()

    def test_controls_and_values_are_scoped_to_one_device_and_home(self):
        result = build_items("Desk Light dim 42", self.store)
        self.assertEqual(len(result), 1)
        action = json.loads(result[0]["arg"])
        self.assertEqual(action, {"action": "control", "profile_id": "default", "home_id": "home-1", "id": "light-1", "command": "dim", "value": "42"})
        self.assertFalse(build_items("Desk Light dim 101", self.store)[0]["valid"])
        self.assertFalse(build_items("Desk Light on extra", self.store)[0]["valid"])

    def test_duplicate_names_require_explicit_selection(self):
        devices = normalize({"data": [
            {"endpoint_id": "a", "device_name": "Lamp", "device_type": "Light", "position_name": "Office"},
            {"endpoint_id": "b", "device_name": "Lamp", "device_type": "Light", "position_name": "Bedroom"},
            {"endpoint_id": "c", "device_name": "Lamp", "device_type": "Light", "position_name": "Bedroom"},
        ]}, "devices")
        self.store.write("catalog", {"home_id": "home-1", "devices": devices, "scenes": []})
        items = build_items("Lamp on", self.store)
        self.assertEqual(len(items), 3)
        self.assertTrue(all(not item["valid"] for item in items))
        self.assertEqual(len({item["autocomplete"] for item in items}), 3)
        for device in devices:
            action = json.loads(build_items(device["label"] + " on", self.store)[0]["arg"])
            self.assertEqual(action["id"], device["id"])

    def test_longest_name_does_not_select_shorter_device(self):
        devices = normalize({"data": [
            {"endpoint_id": "a", "device_name": "Desk", "device_type": "Light"},
            {"endpoint_id": "b", "device_name": "Desk Light", "device_type": "Light"},
        ]}, "devices")
        self.store.write("catalog", {"home_id": "home-1", "devices": devices})
        action = json.loads(build_items("Desk Light on", self.store)[0]["arg"])
        self.assertEqual(action["id"], "b")

    def test_sensor_only_offers_status(self):
        items = build_items("Temperature", self.store)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Show status")
        self.assertIn("Temperature", items[0]["subtitle"])
        self.assertEqual(items[0]["autocomplete"], "Temperature status")
        self.assertFalse(items[0]["valid"])

    def test_status_is_explicit_and_briefly_cached(self):
        self.client.request.return_value = {"data": {"temperature": 22, "humidity": 48}}
        with patch.object(self.store, "client", return_value=self.client):
            for _ in range(2):
                items = build_items("Temperature status", self.store)
                self.assertTrue(all(not item["valid"] for item in items))
                self.assertEqual(items[0]["title"], "Temperature")
                self.assertIn("22", items[0]["subtitle"])
        self.client.request.assert_called_once_with("device/status/query", {"device_ids": ["sensor-1"]})

    def test_live_status_table_displays_readings_without_internal_ids(self):
        self.client.request.return_value = {"code": 0, "message": "", "result": [
            ["endpoint id", "endpoint name", "device name", "device type", "position name", "position id", "status"],
            ["sensor-1", "Temperature", "Temperature and Humidity", "TemperatureSensor", "Office", "room-1", {"temperature": 22}]]}
        with patch.object(self.store, "client", return_value=self.client):
            items = build_items("Temperature status", self.store)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Temperature")
        self.assertIn("22", items[0]["subtitle"])
        self.assertNotIn("sensor-1", json.dumps(items))
        self.assertNotIn("room-1", json.dumps(items))

    def test_serialized_lock_status_is_displayed_in_the_device_subtitle(self):
        self.client.request.return_value = {"code": 0, "result": [
            ["endpoint id", "status"],
            ["sensor-1", "{'lock_state': 'Locked', 'door_state': 'Closed', 'online_offline': 'online'}"]]}
        with patch.object(self.store, "client", return_value=self.client):
            items = build_items("Temperature status", self.store)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Temperature")
        self.assertIn("Lock: Locked", items[0]["subtitle"])
        self.assertTrue(all("{" not in item["subtitle"] and not item["valid"] for item in items))
        self.assertEqual(items[0]["autocomplete"], "Temperature ")

    def test_status_strings_are_not_executed(self):
        value = "{'state': __import__('os').system('false')}"
        self.client.request.return_value = {"data": {"status": value}}
        with patch.object(self.store, "client", return_value=self.client), patch.object(common.os, "system") as execute:
            items = build_items("Temperature status", self.store)
        execute.assert_not_called()
        self.assertEqual(items[0]["subtitle"], "Status: Unavailable")
        self.assertNotIn(value, json.dumps(items))

    def test_lock_status_uses_readable_labels_and_hides_internal_flags(self):
        self.client.request.return_value = {"code": 0, "result": [
            ["endpoint id", "status"],
            ["sensor-1", json.dumps({"remote_control_switch": "-1", "door_state": "DoorClosed",
                                    "lock_state": "Locked", "online_offline": "online"})]]}
        with patch.object(self.store, "client", return_value=self.client):
            items = build_items("Temperature status", self.store)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["subtitle"], "Door: Closed · Lock: Locked · Connection: Online")
        self.assertTrue(all(not item["valid"] for item in items))

    def test_lock_controls_explain_the_service_limitation_without_actions(self):
        devices = normalize({"data": [{"endpoint_id": "lock-1", "endpoint_name": "Front Door", "device_type": "DoorLock"}]}, "devices")
        self.store.write("catalog", {"home_id": "home-1", "devices": devices, "scenes": []})
        items = build_items("Front Door", self.store)
        self.assertEqual(len(items), 2)
        self.assertIn("unavailable", items[1]["subtitle"])
        for command_name in ("lock", "unlock"):
            result = build_items("Front Door " + command_name, self.store)
            self.assertEqual(len(result), 1)
            self.assertFalse(result[0]["valid"])
            self.assertNotIn("arg", result[0])

    def test_execute_revalidates_payload_and_invalidates_status(self):
        self.store.write("status", {"old": {}})
        action = json.loads(build_items("Curtain open", self.store)[0]["arg"])
        with patch.object(self.store, "client", return_value=self.client):
            result = command.execute(action, self.store)
        self.assertEqual(result, "Opening Curtain.")
        self.client.request.assert_called_once_with("device/control", {"device_ids": ["shade-1"], "attribute": "percentage", "action": "set", "value": "100"})
        self.assertEqual(self.store.read("status"), {})
        action["value"] = "extra"
        with patch.object(self.store, "client", return_value=self.client), self.assertRaises(AqaraError):
            command.execute(action, self.store)
        self.assertEqual(self.client.request.call_count, 1)

    def test_stale_home_selection_never_sends_control(self):
        action = json.loads(build_items("Desk Light on", self.store)[0]["arg"])
        self.store.write("settings", {"home_id": "home-2"})
        with patch.object(self.store, "client", return_value=self.client), self.assertRaises(AqaraError):
            command.execute(action, self.store)
        self.client.request.assert_not_called()

    def test_removed_device_never_sends_control(self):
        with self.assertRaises(AqaraError):
            command.execute({"action": "control", "home_id": "home-1", "id": "unknown", "command": "on"}, self.store)

    def test_scene_search_and_execution(self):
        action = json.loads(build_items("scene night", self.store)[0]["arg"])
        with patch.object(self.store, "client", return_value=self.client):
            result = command.execute(action, self.store)
        self.client.request.assert_called_once_with("scene/run", {"scene_ids": ["scene-1"]})
        self.assertEqual(result, "Running “Good Night”.")

    def test_failed_refresh_preserves_catalog(self):
        previous = self.store.read("catalog")
        self.client.request.side_effect = [HOMES, DEVICES, AqaraError("offline")]
        with self.assertRaises(AqaraError):
            self.store.refresh(self.client)
        self.assertEqual(self.store.read("catalog"), previous)

    def test_unknown_response_preserves_catalog(self):
        previous = self.store.read("catalog")
        self.client.request.side_effect = [HOMES, {"data": {"unexpected": []}}]
        with self.assertRaises(AqaraError):
            self.store.refresh(self.client)
        self.assertEqual(self.store.read("catalog"), previous)

    def test_live_agent_tables_normalize_by_header_not_column_order(self):
        homes = normalize({"code": 0, "result": [["home name", "home id"], ["My Home", "home-1"]]}, "homes")
        self.assertEqual(homes, [{"name": "My Home", "id": "home-1"}])
        devices = normalize({"code": 0, "result": [
            ["device name", "device type", "endpoint id", "position name", "endpoint name"],
            ["Wall Switch", "Light", "light-1", "Office", "Desk Light"]]}, "devices")
        self.assertEqual(devices[0]["id"], "light-1")
        self.assertEqual(devices[0]["room"], "Office")
        self.assertEqual(devices[0]["name"], "Desk Light")
        self.assertEqual(devices[0]["device_name"], "Wall Switch")
        scenes = normalize({"code": 0, "result": [["scene name", "scene id"], ["Good Night", "scene-1"]]}, "scenes")
        self.assertEqual(scenes[0]["id"], "scene-1")

    def test_empty_agent_table_is_an_empty_catalog(self):
        self.assertEqual(normalize({"result": [["scene name", "scene id"]]}, "scenes"), [])

    def test_malformed_agent_table_does_not_replace_catalog(self):
        previous = self.store.read("catalog")
        self.client.request.side_effect = [HOMES, {"result": [["device name", "endpoint id"], ["Desk Light"]]}]
        with self.assertRaises(AqaraError):
            self.store.refresh(self.client)
        self.assertEqual(self.store.read("catalog"), previous)

    def test_failed_action_is_visible_in_the_alfred_debugger(self):
        with patch.object(command, "execute", side_effect=AqaraError("Finding homes: account problem")), \
                patch.object(command.sys, "argv", ["command.py", '{"action":"update"}']), \
                patch.object(command.sys, "stdout", new_callable=io.StringIO) as stdout, \
                patch.object(command.sys, "stderr", new_callable=io.StringIO) as stderr:
            command.main()
        self.assertIn("account problem", stdout.getvalue())
        self.assertEqual(stdout.getvalue(), stderr.getvalue())

    def test_refresh_selects_single_home(self):
        self.store.write("settings", {})
        self.client.request.side_effect = [HOMES, DEVICES, SCENES]
        self.assertEqual(self.store.refresh(self.client), (4, 1))
        self.assertEqual(self.store.read("settings")["home_id"], "home-1")

    def test_multiple_homes_require_choice_and_clear_previous_account(self):
        self.client.request.return_value = {"data": [{"home_id": "h2", "name": "Second"}, {"home_id": "h3", "name": "Third"}]}
        with self.assertRaisesRegex(AqaraError, "Choose a home"):
            self.store.refresh(self.client)
        self.assertEqual(self.store.read("catalog"), {})
        self.assertNotIn("home_id", self.store.read("settings"))
        homes = [item for item in build_items("home", self.store) if item.get("arg") and json.loads(item["arg"])["action"] == "home"]
        self.assertEqual(len(homes), 2)
        self.assertEqual(self.store.select_home("h2"), "Second")

    def test_replacing_key_clears_prior_account(self):
        with patch.object(command, "prompt_key", return_value="new-key"):
            command.execute({"action": "apikey"}, self.store)
        self.keys.set.assert_called_once_with("new-key")
        self.assertEqual(self.store.read("catalog"), {})
        self.assertNotIn("home_id", self.store.read("settings"))
        self.assertNotIn("new-key", "".join(p.read_text() for p in Path(self.temp.name).glob("*.json")))

    def test_cancelled_key_dialog_keeps_catalog(self):
        with patch.object(command, "prompt_key", return_value=None):
            command.execute({"action": "apikey"}, self.store)
        self.keys.set.assert_not_called()
        self.assertTrue(self.store.read("catalog"))

    def test_system_named_device_can_be_selected(self):
        devices = normalize({"data": [{"endpoint_id": "a", "device_name": "home", "device_type": "Light"}]}, "devices")
        self.store.write("catalog", {"home_id": "home-1", "devices": devices})
        action = json.loads(build_items("device home on", self.store)[0]["arg"])
        self.assertEqual(action["id"], "a")

    def test_special_characters_are_data(self):
        name = 'Bob\'s "Lamp" $(touch /tmp/no) `false`'
        devices = normalize({"data": [{"endpoint_id": "a", "device_name": name, "device_type": "Light"}]}, "devices")
        self.store.write("catalog", {"home_id": "home-1", "devices": devices})
        action = json.loads(build_items(name + " on", self.store)[0]["arg"])
        self.assertEqual(action["id"], "a")
        self.assertEqual(build_items("Bob", self.store)[0]["autocomplete"], name + " status")
        self.assertTrue(all(name in item["subtitle"] for item in build_items("Bob", self.store)))

    def test_unique_partial_name_expands_commands_with_distinct_action_titles(self):
        items = build_items("Desk", self.store)
        self.assertGreater(len(items), 1)
        self.assertEqual(len({item["title"] for item in items}), len(items))
        self.assertTrue(all("Desk Light" in item["subtitle"] for item in items))
        on = next(item for item in items if item.get("arg") and json.loads(item["arg"])["command"] == "on")
        self.assertEqual(on["title"], "Turn on")
        self.assertEqual(on["autocomplete"], "Desk Light on")
        self.keys.get.assert_not_called()

    def test_partial_device_and_command_autocomplete_to_the_full_command(self):
        items = build_items("Desk o", self.store)
        self.assertEqual({item["autocomplete"] for item in items}, {"Desk Light on", "Desk Light off"})
        self.assertTrue(all(item["valid"] for item in items))
        self.assertEqual({item["title"] for item in items}, {"Turn on", "Turn off"})

    def test_numeric_command_autocomplete_preserves_the_entered_value(self):
        item = build_items("Desk dim 42", self.store)[0]
        self.assertEqual(item["title"], "Set brightness to 42%")
        self.assertEqual(item["autocomplete"], "Desk Light dim 42")
        self.assertEqual(json.loads(item["arg"])["value"], "42")
        missing = build_items("Desk dim", self.store)[0]
        self.assertFalse(missing["valid"])
        self.assertEqual(missing["autocomplete"], "Desk Light dim ")

    def test_mode_and_color_values_have_autocomplete_suggestions(self):
        modes = build_items("AC mode", self.store)
        self.assertEqual({json.loads(item["arg"])["value"] for item in modes}, {"auto", "heat", "cool", "dry", "fan"})
        cool = build_items("AC mode co", self.store)
        self.assertEqual(len(cool), 1)
        self.assertEqual(cool[0]["autocomplete"], "AC mode cool")
        self.assertEqual(cool[0]["title"], "Set mode to cool")
        blue = build_items("Desk color bl", self.store)
        self.assertEqual(len(blue), 1)
        self.assertEqual(blue[0]["autocomplete"], "Desk Light color blue")
        self.assertEqual(blue[0]["title"], "Set color to blue")

    def test_partial_longer_name_does_not_pick_a_shorter_named_device(self):
        devices = normalize({"data": [
            {"endpoint_id": "a", "device_name": "Desk", "device_type": "Light"},
            {"endpoint_id": "b", "device_name": "Desk Light", "device_type": "Light"},
        ]}, "devices")
        self.store.write("catalog", {"home_id": "home-1", "devices": devices})
        items = build_items("Desk L", self.store)
        self.assertTrue(items)
        self.assertTrue(all("Desk Light" in item["subtitle"] for item in items))

    def test_automatic_status_is_one_subtitle_row_before_commands(self):
        self.store.write("settings", {"home_id": "home-1", "showstatus": True})
        self.client.request.return_value = {"data": {"on_off": "on", "brightness": 50}}
        with patch.object(self.store, "client", return_value=self.client):
            items = build_items("Desk", self.store)
        self.assertEqual(items[0]["title"], "Desk Light")
        self.assertIn("Power: On", items[0]["subtitle"])
        self.assertIn("Brightness: 50", items[0]["subtitle"])
        self.assertFalse(items[0]["valid"])
        self.assertFalse(any(item["title"] == "Show status" for item in items))

    def test_store_writes_private_files(self):
        self.assertEqual((Path(self.temp.name) / "settings.json").stat().st_mode & 0o777, 0o600)

    def add_second_account(self):
        target = self.store.add_profile("US", "us-test-key")
        target.write("settings", {"home_id": "home-1", "home_name": "US Home",
                                  "homes": [{"id": "home-1", "name": "US Home"}]})
        devices = normalize({"data": [{"endpoint_id": "light-1", "endpoint_name": "US Lamp", "device_type": "Light"}]}, "devices")
        target.write("catalog", {"home_id": "home-1", "devices": devices, "scenes": []})
        return target

    def test_legacy_account_and_credential_work_without_migration(self):
        self.assertEqual(self.store.profile_id, "default")
        self.assertEqual(self.store.directory, Path(self.temp.name))
        self.assertIs(self.store.keychain, self.keys)
        self.assertTrue(self.store.read("catalog")["devices"])
        self.assertFalse((self.store.root / "accounts.json").exists())

    def test_switching_homes_preserves_each_catalog(self):
        homes = [{"id": "home-1", "name": "First Home"}, {"id": "home-2", "name": "Second Home"}]
        self.store.write("settings", {"home_id": "home-1", "home_name": "First Home", "homes": homes})
        first_catalog = self.store.read("catalog")
        self.store.select_home("home-2")
        self.assertEqual(self.store.read("catalog"), {})
        second_catalog = {"home_id": "home-2", "devices": [{"id": "other", "name": "Second Lamp", "type": "Light"}], "scenes": []}
        self.store.write("catalog", second_catalog)
        self.store.select_home("home-1")
        self.assertEqual(self.store.read("catalog"), first_catalog)
        self.store.select_home("home-2")
        self.assertEqual(self.store.read("catalog"), second_catalog)
        self.keys.get.assert_not_called()

    def test_home_picker_lists_all_homes_in_all_accounts(self):
        self.store.write("settings", {"home_id": "home-1", "home_name": "My Home", "homes": [
            {"id": "home-1", "name": "My Home"}, {"id": "home-2", "name": "Vacation"}]})
        target = self.add_second_account()
        items = build_items("homes", self.store)
        homes = [item for item in items if item.get("arg") and json.loads(item["arg"])["action"] == "home"]
        self.assertEqual(len(homes), 3)
        self.assertIn("Current home", homes[0]["subtitle"])
        us = build_items("home US", self.store)[0]
        self.assertEqual(json.loads(us["arg"])["profile_id"], target.profile_id)
        self.assertIn("US", us["subtitle"])
        self.keys.get.assert_not_called()

    def test_home_selection_switches_account_and_uses_its_saved_key(self):
        target = self.add_second_account()
        action = json.loads(build_items("home US", self.store)[0]["arg"])
        result = command.execute(action, self.store)
        active = Store(self.temp.name, keychain_factory=self.keychain_factory)
        self.assertEqual(active.profile_id, target.profile_id)
        self.assertEqual(active.client().key, "us-test-key")
        self.assertIn("US Home", result)
        self.assertEqual(build_items("US Lamp on", active)[0]["valid"], True)
        self.assertEqual(self.store.read("catalog")["devices"], self.devices)

    def test_old_actions_are_rejected_across_accounts_with_identical_ids(self):
        action = json.loads(build_items("Desk Light on", self.store)[0]["arg"])
        target = self.add_second_account()
        target.activate()
        active = Store(self.temp.name, keychain_factory=self.keychain_factory)
        with patch.object(active, "client", return_value=self.client), self.assertRaises(AqaraError):
            command.execute(action, active)
        self.client.request.assert_not_called()
        # A profile switch after a command process starts is rejected too.
        with patch.object(self.store, "client", return_value=self.client), self.assertRaises(AqaraError):
            command.execute(action, self.store)

    def test_renaming_account_keeps_credential_and_homes(self):
        original = self.store.read("catalog")
        self.store.rename_profile("Personal")
        self.assertEqual(self.store.profile_name(), "Personal")
        self.assertEqual(self.store.read("catalog"), original)
        self.assertIs(self.store.keychain, self.keys)
        self.keys.set.assert_not_called()

    def test_rename_picker_can_rename_an_inactive_account_without_a_network_call(self):
        target = self.add_second_account()
        items = build_items("account rename", self.store)
        action = next(json.loads(item["arg"]) for item in items
                      if json.loads(item["arg"])["profile_id"] == target.profile_id)
        self.assertEqual(action["action"], "account_rename_prompt")
        with patch.object(command, "prompt_profile_name", return_value="Work"):
            result = command.execute(action, self.store)
        self.assertEqual(target.profile_name(), "Work")
        self.assertEqual(self.store.active_profile_id(), "default")
        self.assertIn("Work", result)
        self.profile_keys[target.profile_id].get.assert_not_called()

    def test_cancelling_a_rename_keeps_the_name(self):
        with patch.object(command, "prompt_profile_name", return_value=None):
            result = command.execute({"action": "account_rename_prompt", "profile_id": "default"}, self.store)
        self.assertEqual(self.store.profile_name(), "Current account")
        self.assertIn("cancelled", result)

    def test_api_keys_are_rejected_as_account_labels(self):
        key = "a1b2c3d4" * 4
        with patch.object(command, "prompt_key") as prompt, self.assertRaises(AqaraError):
            command.execute({"action": "account_add", "name": "Work " + key}, self.store)
        prompt.assert_not_called()
        with self.assertRaises(AqaraError):
            self.store.add_profile("Work " + "x" * 30, "x" * 30)
        self.assertEqual(len(self.store.profiles()), 1)

    def test_failed_home_switch_restores_the_previous_home_and_cache(self):
        self.store.write("settings", {"home_id": "home-1", "home_name": "First", "homes": [
            {"id": "home-1", "name": "First"}, {"id": "home-2", "name": "Second"}]})
        self.store.write("status", {"cached": {"data": "before"}})
        before = {name: self.store.read(name) for name in ("settings", "catalog", "catalogs", "status")}
        with patch.object(self.store, "refresh", side_effect=AqaraError("Unavailable")), self.assertRaises(AqaraError):
            self.store.switch_home("home-2")
        after = {name: self.store.read(name) for name in before}
        self.assertEqual(after, before)

    def test_failed_account_switch_does_not_change_the_active_account(self):
        target = self.add_second_account()
        target.clear_catalog()
        self.client.request.side_effect = AqaraError("Unavailable")
        with patch.object(common, "Client", return_value=self.client), self.assertRaises(AqaraError):
            command.execute({"action": "account", "profile_id": target.profile_id}, self.store)
        self.assertEqual(self.store.active_profile_id(), "default")
        self.assertEqual(self.store.read("catalog")["devices"], self.devices)

    def test_account_add_validates_key_before_saving_profile(self):
        self.client.request.side_effect = AqaraError("Unauthorized")
        with patch.object(command, "prompt_key", return_value="bad-key"), \
                patch.object(command, "Client", return_value=self.client), self.assertRaises(AqaraError):
            command.execute({"action": "account_add", "name": "US"}, self.store)
        self.assertEqual(len(self.store.profiles()), 1)
        self.keys.set.assert_not_called()

    def test_connecting_multiple_homes_waits_for_an_explicit_choice(self):
        self.client.request.return_value = {"result": [["home name", "home id"], ["First", "a"], ["Second", "b"]]}
        with patch.object(command, "prompt_key", return_value="us-test-key"), \
                patch.object(command, "Client", return_value=self.client):
            result = command.execute({"action": "account_add", "name": "US"}, self.store)
        active = Store(self.temp.name, keychain_factory=self.keychain_factory)
        self.assertEqual(active.profile_name(), "US")
        self.assertNotIn("home_id", active.read("settings"))
        self.assertEqual(len(active.read("settings")["homes"]), 2)
        self.assertIn("2 homes", result)
        self.assertTrue(all(call.args[0] == "homes/query" for call in self.client.request.call_args_list))

    def test_replacing_one_accounts_key_preserves_other_accounts(self):
        target = self.add_second_account()
        with patch.object(command, "prompt_key", return_value="replacement"):
            command.execute({"action": "apikey"}, self.store)
        self.assertEqual(len(self.store.profiles()), 2)
        self.assertTrue(target.read("catalog")["devices"])
        self.assertEqual(target.client().key, "us-test-key")

    def test_account_names_are_unique_without_putting_names_in_paths(self):
        target = self.store.add_profile("../US", "us-test-key")
        self.assertEqual(target.directory.parent, self.store.root / "profiles")
        with self.assertRaises(AqaraError):
            self.store.add_profile("../us", "other-key")
        with self.assertRaises(AqaraError):
            self.store.for_profile("../../outside")

    def test_status_caches_are_separate_even_when_home_and_device_ids_match(self):
        target = self.add_second_account()
        first_client, second_client = Mock(), Mock()
        first_client.request.return_value = {"data": {"on_off": "on"}}
        second_client.request.return_value = {"data": {"on_off": "off"}}
        first_device = next(device for device in self.devices if device["id"] == "light-1")
        second_device = target.read("catalog")["devices"][0]
        self.assertEqual(self.store.status(first_device, first_client)["data"]["on_off"], "on")
        self.assertEqual(target.status(second_device, second_client)["data"]["on_off"], "off")
        self.assertEqual(self.store.status(first_device, first_client)["data"]["on_off"], "on")
        first_client.request.assert_called_once()
        second_client.request.assert_called_once()

    def test_home_refresh_is_partial_when_one_account_is_unavailable(self):
        target = self.add_second_account()
        first, second = Mock(), Mock()
        first.discover_homes.return_value = [{"id": "a"}, {"id": "b"}]
        second.discover_homes.side_effect = AqaraError("Authorization failed")
        with patch.object(self.store, "for_profile", side_effect=lambda identifier: first if identifier == "default" else second):
            result = command.execute({"action": "homes_refresh"}, self.store)
        self.assertIn("2 homes across 1 accounts", result)
        self.assertIn("US: Authorization failed", result)
        self.assertTrue(target.read("catalog"))

    def test_new_home_queries_the_selected_account_with_its_home_header(self):
        target = self.add_second_account()
        target.write("settings", {"homes": [{"id": "us-new", "name": "New US Home"}]})
        replies = [
            {"result": [["home name", "home id"], ["New US Home", "us-new"]]},
            DEVICES, SCENES,
        ]
        requests = []
        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                return io.BytesIO(json.dumps(replies.pop(0)).encode())
        client = Client("us-test-key", opener=Opener())
        with patch.object(common, "Client", return_value=client):
            result = command.execute({"action": "home", "profile_id": target.profile_id, "id": "us-new"}, self.store)
        self.assertIn("New US Home", result)
        self.assertEqual(len(requests), 3)
        for request in requests:
            self.assertEqual(request.get_header("Authorization"), "Bearer us-test-key")
        self.assertIsNone(requests[0].get_header("Position_id"))
        self.assertEqual(requests[1].get_header("Position_id"), "us-new")
        self.assertEqual(requests[2].get_header("Position_id"), "us-new")

    def test_reset_removes_all_accounts_credentials_and_cached_data(self):
        target = self.add_second_account()
        command.execute({"action": "reinit"}, self.store)
        self.keys.delete.assert_called_once()
        self.profile_keys[target.profile_id].delete.assert_called_once()
        self.assertFalse((self.store.root / "accounts.json").exists())
        self.assertEqual(self.store.read("catalog"), {})
        self.assertEqual(target.read("catalog"), {})


class ControlTest(unittest.TestCase):
    def test_ranges_and_unsupported_controls(self):
        light = {"id": "a", "type": "Light"}
        for name, value in (("dim", "0"), ("dim", "101"), ("dim", "1.5"), ("ct", "2699"), ("ct", "6501"), ("color", ""), ("on", "extra")):
            with self.subTest(name=name, value=value), self.assertRaises(AqaraError):
                control_body(light, name, value)
        with self.assertRaises(AqaraError):
            control_body({"id": "b", "type": "TemperatureSensor"}, "on")
        with self.assertRaises(AqaraError):
            control_body({"id": "b", "type": "DoorLock,Switch"}, "on")
        with self.assertRaises(AqaraError):
            control_body({"id": "b", "type": "AirConditioner"}, "mode", "invalid")
        self.assertEqual(control_body(light, "on"), {"device_ids": ["a"], "attribute": "on_off", "action": "on"})
        self.assertEqual(control_body({"id": "b", "type": "AirConditioner"}, "mode", "cool")["attribute"], "ac_mode")


class ClientTest(unittest.TestCase):
    def request_with(self, payload, path="homes/query", body=None):
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(payload).encode())
        client = Client("test-key", "home-1", opener)
        result = client.request(path, body)
        return result, opener.open.call_args[0][0]

    def test_authentication_and_home_headers(self):
        _, req = self.request_with(HOMES)
        headers = {key.lower(): value for key, value in req.header_items()}
        self.assertEqual(headers["authorization"], "Bearer test-key")
        self.assertNotIn("position_id", headers)
        _, req = self.request_with({"code": 0}, "device/control", {"device_ids": ["x"], "attribute": "on_off", "action": "on"})
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Position_id"), "home-1")
        self.assertEqual(json.loads(req.data)["device_ids"], ["x"])

    def test_application_failures_and_partial_failures_are_not_success(self):
        for payload in ({"code": 500}, {"success": False}, {"error": "oops"},
                        {"code": 0, "data": [{"success": False}]},
                        {"code": 0, "result": {"status": "failed"}}):
            with self.subTest(payload=payload), self.assertRaises(AqaraError):
                self.request_with(payload)

    def test_live_empty_scene_response_becomes_an_empty_scene_catalog(self):
        payload = {"code": -1, "message": "Scene base inquiry failed: No scene found at home", "result": {}}
        response, _ = self.request_with(payload, "home/scenes/query")
        self.assertEqual(normalize(response, "scenes"), [])

    def test_empty_scene_handling_does_not_hide_other_failures(self):
        for payload, path in (
            ({"code": -1, "message": "Scene base inquiry failed: No scene found at home", "result": {}}, "home/devices/query"),
            ({"code": -1, "message": "No scene found at home: unauthorized", "result": {}}, "home/scenes/query"),
            ({"code": -1, "message": "Scene base inquiry failed: No scene found at home", "result": {"failure": True}}, "home/scenes/query"),
        ):
            with self.subTest(path=path, payload=payload), self.assertRaises(AqaraError):
                self.request_with(payload, path)

    def test_no_homes_error_explains_the_account_problem(self):
        # Observed from the live homes API when this account had no visible homes.
        payload = {"result": {}, "code": -1,
                   "message": "All homes inquiry failed: ERROR.NoHomesUnderAccount", "request_id": "example"}
        with self.assertRaises(AqaraError) as caught:
            self.request_with(payload)
        self.assertIn("Finding homes", str(caught.exception))
        self.assertIn("NoHomesUnderAccount", str(caught.exception))
        self.assertIn("account and region", str(caught.exception))

    def test_api_error_details_do_not_echo_credentials(self):
        with self.assertRaises(AqaraError) as caught:
            self.request_with({"code": -7, "message": "Rejected Authorization: Bearer test-key"})
        self.assertIn("code -7", str(caught.exception))
        self.assertNotIn("test-key", str(caught.exception))
        self.assertNotIn("Bearer", str(caught.exception))

    def test_api_auth_failures_explain_how_to_reconnect(self):
        with self.assertRaisesRegex(AqaraError, "aq login, then aq apikey"):
            self.request_with({"code": -1, "message": "unauthorized or insufficient permissions"})

    def test_http_errors_do_not_leak_response_or_secret(self):
        for code in (401, 403, 429, 500, 302):
            opener = Mock()
            opener.open.side_effect = HTTPError("https://example.com", code, "private response", {}, io.BytesIO(b"private response"))
            with self.subTest(code=code), self.assertRaises(AqaraError) as caught:
                Client("test-key", "h", opener).request("homes/query")
            self.assertNotIn("test-key", str(caught.exception))
            self.assertNotIn("private response", str(caught.exception))

    def test_timeout_does_not_retry_mutation(self):
        opener = Mock()
        opener.open.side_effect = URLError("timeout")
        with self.assertRaisesRegex(AqaraError, "may still have run"):
            Client("test-key", "h", opener).request("device/control", {})
        opener.open.assert_called_once()

    def test_missing_home_blocks_non_home_requests(self):
        with self.assertRaises(AqaraError):
            Client("test-key").request("device/control", {})

    def test_bad_json(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(b"not json")
        with self.assertRaisesRegex(AqaraError, "unreadable"):
            Client("test-key", opener=opener).request("homes/query")

    def test_redirects_are_rejected(self):
        self.assertIsNone(common.NoRedirects().redirect_request(None, None, 302, "", {}, "https://other.example"))


class PackageTest(unittest.TestCase):
    def test_workflow_graph_and_runner_use_argv(self):
        root = Path(__file__).resolve().parent.parent
        info = plistlib.loads((root / "info.plist").read_bytes())
        objects = {obj["uid"]: obj for obj in info["objects"]}
        self.assertEqual(info["bundleid"], common.BUNDLE_ID)
        for source, edges in info["connections"].items():
            self.assertIn(source, objects)
            for edge in edges:
                self.assertIn(edge["destinationuid"], objects)
        for obj in objects.values():
            # Object schema versions differ by type in Alfred 5.8.1.
            supported_versions = {"alfred.workflow.input.scriptfilter": 3,
                                  "alfred.workflow.action.script": 2,
                                  "alfred.workflow.output.notification": 1}
            self.assertLessEqual(obj["version"], supported_versions[obj["type"]])
            config = obj["config"]
            if "script" in config:
                self.assertEqual(config["scriptargtype"], 1)
                self.assertIn('"$1"', config["script"])
                self.assertNotIn("{query}", config["script"])

    def test_package_allowlist_and_importable_plist(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "Aqara.alfredworkflow"
            subprocess.run(["python3", str(root / "workflow-build"), str(archive)], check=True, capture_output=True)
            with ZipFile(archive) as package:
                self.assertEqual(set(package.namelist()), {
                    "info.plist", "icon.png", "workflow.sh", "filter.py", "command.py", "common.py", "keychain.py",
                    "updater.py", "README.md", "LICENSE", "version", "workflow/__init__.py", "workflow/workflow.py",
                    "workflow/util.py", "workflow/web.py", "workflow/update.py", "workflow/background.py", "workflow/version", "workflow/LICENSE"})
                plistlib.loads(package.read("info.plist"))
                self.assertEqual(package.getinfo("workflow.sh").external_attr >> 16 & 0o777, 0o755)


if __name__ == "__main__":
    unittest.main()
