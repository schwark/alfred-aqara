import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import command
from common import AqaraError, Store
from cloud_backend import BUNDLE, CloudStore
from filter import build_items
from keychain import KeychainError
import runtime
from updater import Updater


class ModeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "workflow"
        self.environment = patch.dict(os.environ, {"alfred_workflow_data": str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        # Mode tests must never inspect the user's legacy data or Keychain.
        migration = patch("runtime.migrate_cloud", return_value=False)
        self.migration = migration.start()
        self.addCleanup(migration.stop)
        self.updater = Mock()
        self.updater.available.return_value = None

    def seed(self, store, name="Desk Light"):
        store.write("settings", {"home_id": "example-home", "home_name": "Example Home"})
        backend = {"backend": "mobile", "did": "example-device"} if store.mode == "cloud" else {}
        store.write("catalog", {"home_id": "example-home", "devices": [
            {"id": "example-light", "name": name, "type": "Light", "room": "Office", **backend}], "scenes": []})

    def test_default_is_agent_and_legacy_environment_does_not_select_cloud(self):
        with patch.dict(os.environ, {"AQARA_BACKEND": "mobile"}):
            store = runtime.get_store()
        self.assertEqual(store.mode, "agent")
        self.assertEqual(store.root, self.root)
        self.assertFalse((self.root / "mode.json").exists())
        self.migration.assert_not_called()

    def test_picker_and_switch_work_even_with_an_invalid_account_selection(self):
        Store._write_path(self.root / "accounts.json", {"active_id": "missing", "profiles": []})
        items = build_items("mode ?")
        self.assertEqual([i["title"] for i in items], ["Agent mode", "Cloud mode"])
        self.assertEqual(items[0]["subtitle"], "Current mode")
        self.assertEqual(items[1]["autocomplete"], "mode cloud")
        self.assertEqual(len(build_items("mode cl")), 1)
        self.assertIn("Cloud mode", command.execute(json.loads(items[1]["arg"])))
        self.assertEqual(runtime.mode_state()[0], "cloud")
        self.assertEqual(build_items("mode cloud")[0]["subtitle"], "Current mode")
        self.assertEqual(runtime.get_store().mode, "cloud")
        command.execute({"action": "mode", "value": "agent"})
        self.assertEqual(runtime.mode_state()[0], "agent")

    def test_switch_preserves_each_modes_catalog_preferences_and_account_selection(self):
        agent = runtime.get_store()
        self.seed(agent)
        agent.write("settings", {"home_id": "agent-home", "showstatus": True})
        runtime.set_mode("cloud")
        cloud = runtime.get_store()
        self.assertIsInstance(cloud, CloudStore)
        self.assertEqual(cloud.root, self.root / "cloud")
        self.seed(cloud, "Cloud Light")
        cloud.write("settings", {"home_id": "cloud-home", "showstatus": False})
        runtime.set_mode("agent")
        restored = runtime.get_store()
        self.assertEqual(restored.read("settings"), {"home_id": "agent-home", "showstatus": True})
        self.assertEqual(restored.read("catalog")["devices"][0]["name"], "Desk Light")
        runtime.set_mode("cloud")
        restored = runtime.get_store()
        self.assertEqual(restored.read("settings"), {"home_id": "cloud-home", "showstatus": False})
        self.assertEqual(restored.read("catalog")["devices"][0]["name"], "Cloud Light")

    def test_old_command_is_rejected_after_switch_and_after_switching_back(self):
        agent = runtime.get_store()
        self.seed(agent)
        action = json.loads(build_items("Desk Light on", agent, self.updater)[0]["arg"])
        runtime.set_mode("cloud")
        cloud = runtime.get_store()
        with patch.object(cloud, "control") as write, self.assertRaisesRegex(AqaraError, "mode changed"):
            command.execute(action, cloud)
        write.assert_not_called()
        runtime.set_mode("agent")
        restored = runtime.get_store()
        with patch.object(restored, "control") as write, self.assertRaisesRegex(AqaraError, "mode changed"):
            command.execute(action, restored)
        write.assert_not_called()

    def test_switch_while_lock_password_is_open_never_writes(self):
        runtime.set_mode("cloud")
        cloud = runtime.get_store()
        device = {"id": "example-lock", "did": "example-device", "endpoint": "7", "name": "Example Lock",
                  "backend": "mobile", "type": "DoorLock", "mobile_commands": {"unlock": "7.148.35011.0"}}
        cloud.write("settings", {"home_id": "example-home"})
        cloud.write("catalog", {"home_id": "example-home", "devices": [device], "scenes": []})
        client = Mock()
        client.endpoint_capabilities.return_value = ([{"endpoint": "7", "commands": device["mobile_commands"]}], 0)

        def switch(*args, **kwargs):
            runtime.set_mode("agent")
            return ""

        action = json.loads(build_items("Example Lock unlock", cloud, self.updater)[0]["arg"])
        with patch.object(cloud, "cloud_client", return_value=client), patch("cloud_backend.dialog", side_effect=switch):
            with self.assertRaisesRegex(AqaraError, "mode changed"):
                command.execute(action, cloud)
        client.write_lock.assert_not_called()

    def test_scene_and_agent_controls_recheck_mode_after_loading_credentials(self):
        agent = runtime.get_store()

        def switch():
            runtime.set_mode("cloud")
            return Mock()

        with patch.object(agent, "client", side_effect=switch) as client:
            with self.assertRaisesRegex(AqaraError, "mode changed"):
                agent.control({"id": "example-light", "type": "Light"}, "on")
            client.return_value.request.assert_not_called()
        runtime.set_mode("agent")
        agent = runtime.get_store()
        client = Mock()

        def switch_scene():
            runtime.set_mode("cloud")
            return client

        with patch.object(agent, "client", side_effect=switch_scene), self.assertRaisesRegex(AqaraError, "mode changed"):
            agent.run_scene({"id": "example-scene"})
        client.request.assert_not_called()

    def test_both_modes_share_updater_directory_and_profile_guards(self):
        agent = runtime.get_store()
        runtime.set_mode("cloud")
        cloud = runtime.get_store()
        self.assertEqual(Updater(agent.workflow_root).directory, Updater(cloud.workflow_root).directory)
        self.assertFalse(Updater(cloud.workflow_root).workflow._update_settings["prereleases"])
        other = cloud.for_profile("default")
        self.assertEqual(other.workflow_root, self.root)
        runtime.set_mode("agent")
        with self.assertRaisesRegex(AqaraError, "mode changed"):
            other.ensure_current_mode()

    def test_invalid_mode_leaves_choice_unchanged(self):
        with self.assertRaises(AqaraError):
            runtime.set_mode("mobile")
        self.assertEqual(runtime.mode_state()[0], "agent")

    def test_reset_only_clears_the_selected_modes_connections(self):
        agent = runtime.get_store()
        self.seed(agent)
        runtime.set_mode("cloud")
        cloud = runtime.get_store()
        self.seed(cloud, "Cloud Light")
        keys = Mock()
        cloud.keychain_factory = lambda identifier: keys
        command.execute({"action": "reinit"}, cloud)
        keys.delete.assert_called_once_with()
        self.assertFalse(cloud.read("catalog"))
        self.assertEqual(agent.read("catalog")["devices"][0]["name"], "Desk Light")
        self.assertEqual(runtime.mode_state()[0], "cloud")

    def test_native_scene_rechecks_mode_after_loading_credentials(self):
        runtime.set_mode("cloud")
        cloud = runtime.get_store()
        client = Mock()

        def switch():
            runtime.set_mode("agent")
            return client

        with patch.object(cloud, "cloud_client", side_effect=switch), self.assertRaisesRegex(AqaraError, "mode changed"):
            cloud.run_scene({"backend": "mobile", "scene_id": "example-scene"})
        client._call.assert_not_called()


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "workflow"
        self.legacy = Path(self.temp.name) / "legacy"
        self.values = {("com.schwark.aqara.mobile", "app-identity"): "example-app-identity",
                       ("com.schwark.aqara.mobile", "connection:default"): '{"token":"example-token"}',
                       ("com.schwark.aqara.mobile", "connection:other"): '{"token":"example-other-token"}'}
        self.original = dict(self.values)
        self.created = {}

        def factory(service, account):
            key = (service, account)
            if key not in self.created:
                item = Mock()
                item.get.side_effect = lambda: self.values.get(key)
                item.set.side_effect = lambda value: self.values.update({key: value})
                item.delete.side_effect = lambda: self.values.pop(key, None)
                self.created[key] = item
            return self.created[key]

        self.factory = factory
        self.keychain = patch("runtime.Keychain", side_effect=factory)
        self.keychain.start()
        self.addCleanup(self.keychain.stop)
        Store._write_path(self.legacy / "accounts.json", {"active_id": "other", "profiles": [
            {"id": "default", "name": "Example Account"}, {"id": "other", "name": "Another Account"}]})
        Store._write_path(self.legacy / "profiles/other/settings.json", {
            "home_id": "mobile:example-home", "home_name": "Example Home (Mobile)",
            "home_links": {"agent-home": "example-home"}, "showstatus": True})
        Store._write_path(self.legacy / "profiles/other/catalog.json", {"home_id": "mobile:example-home", "devices": [
            {"id": "mobile:example-device#7", "backend": "mobile", "name": "Example Lock (Mobile)"}]})

    def test_migration_preserves_sessions_links_and_ids_and_is_idempotent(self):
        agent = Store(self.root, keychain=Mock())
        agent.write("settings", {"home_id": "agent-home"})
        original_files = {p.relative_to(self.legacy): p.read_bytes() for p in self.legacy.rglob("*.json")}
        self.assertTrue(runtime.migrate_cloud(self.root, self.legacy))
        cloud = CloudStore(self.root / "cloud", keychain_factory=lambda i: Mock())
        self.assertEqual(cloud.profile_id, "other")
        self.assertEqual(cloud.read("settings")["home_name"], "Example Home (Cloud)")
        self.assertEqual(cloud.read("settings")["home_links"], {"agent-home": "example-home"})
        self.assertEqual(cloud.read("catalog")["devices"][0], {
            "id": "mobile:example-device#7", "backend": "mobile", "name": "Example Lock (Cloud)",
            "label": "Example Lock (Cloud)"})
        self.assertEqual(self.values[(BUNDLE, "connection:other")], self.original[("com.schwark.aqara.mobile", "connection:other")])
        self.assertEqual(self.values[(BUNDLE, "app-identity")], "example-app-identity")
        self.assertFalse(runtime.migrate_cloud(self.root, self.legacy))
        self.assertEqual(agent.read("settings"), {"home_id": "agent-home"})
        self.assertEqual({p.relative_to(self.legacy): p.read_bytes() for p in self.legacy.rglob("*.json")}, original_files)
        for p in (self.root / "cloud").rglob("*.json"):
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_failed_migration_rolls_back_new_credentials_and_can_be_retried(self):
        item = self.factory(BUNDLE, "connection:other")
        item.set.side_effect = KeychainError("Access cancelled")
        with self.assertRaises(KeychainError):
            runtime.migrate_cloud(self.root, self.legacy)
        self.assertEqual(self.values, self.original)
        self.assertFalse((self.root / "cloud").exists())
        self.assertEqual(runtime.mode_state(self.root)[0], "agent")
        item.set.side_effect = lambda value: self.values.update({(BUNDLE, "connection:other"): value})
        self.assertTrue(runtime.migrate_cloud(self.root, self.legacy))

    def test_existing_cloud_setup_is_never_overwritten(self):
        Store._write_path(self.root / "cloud/settings.json", {"home_id": "new-home"})
        self.assertFalse(runtime.migrate_cloud(self.root, self.legacy))
        self.assertEqual(Store._read_path(self.root / "cloud/settings.json"), {"home_id": "new-home"})
        self.assertEqual(self.values, self.original)

    def test_conflicting_credentials_never_get_combined_with_old_catalogs(self):
        self.values[(BUNDLE, "connection:other")] = "different-existing-credential"
        expected = dict(self.values)
        with self.assertRaisesRegex(AqaraError, "credentials already exist"):
            runtime.migrate_cloud(self.root, self.legacy)
        self.assertEqual(self.values, expected)
        self.assertFalse((self.root / "cloud").exists())


if __name__ == "__main__":
    unittest.main()
