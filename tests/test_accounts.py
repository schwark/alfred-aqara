import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import filter as workflow_filter
from cloud_backend import CloudStore
from common import Store
from filter import build_items


class AccountPickerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.keys = Mock()
        self.updater = Mock()
        self.updater.available.return_value = None
        self.updater.checking.return_value = False

    def seed(self, store_type=Store):
        store = store_type(self.root, keychain_factory=lambda identifier: self.keys)
        store.workflow_root = self.root
        store.write('settings', {'home_id': 'first-home', 'home_name': 'First Home',
                                 'homes': [{'id': 'first-home', 'name': 'First Home'}]})
        other = store.add_profile('Work', 'example-credential')
        other.write('settings', {'home_id': 'work-home', 'home_name': 'Work Home',
                                 'homes': [{'id': 'work-home', 'name': 'Work Home'}]})
        other.activate()
        self.keys.reset_mock()
        return store.for_profile(other.profile_id), other.profile_id

    def accounts(self, query, store):
        return [row for row in build_items(query, store, self.updater)
                if json.loads(row.get('arg', '{}')).get('action') == 'account']

    def test_current_account_title_and_switch_target_title_use_names_in_both_modes(self):
        for store_type in (Store, CloudStore):
            with self.subTest(mode=store_type.mode):
                # Each mode has its own isolated setup and profiles.
                self.root = Path(self.temp.name) / store_type.mode
                store, other_id = self.seed(store_type)
                rows = self.accounts('account', store)
                self.assertEqual([row['title'] for row in rows], ['Work', 'First Home'])
                self.assertEqual(rows[0]['subtitle'], 'Current account · 1 home · Work Home')
                self.assertEqual(rows[1]['subtitle'], 'Switch to this account · 1 home · First Home')
                self.assertEqual(json.loads(rows[0]['arg'])['profile_id'], other_id)
                self.assertEqual(json.loads(rows[1]['arg'])['profile_id'], 'default')
                self.keys.get.assert_not_called()

    def test_legacy_placeholder_is_only_a_display_fallback_without_account_mutation(self):
        store, _ = self.seed()
        accounts = store._read_path(self.root / 'accounts.json')
        accounts['profiles'][0]['name'] = 'Current account'
        store._write_path(self.root / 'accounts.json', accounts)
        before = (self.root / 'accounts.json').read_bytes()
        rows = self.accounts('account', store)
        self.assertEqual(rows[1]['title'], 'First Home')
        self.assertEqual((self.root / 'accounts.json').read_bytes(), before)
        self.keys.get.assert_not_called()

    def test_named_accounts_keep_their_name_when_home_changes(self):
        store, _ = self.seed()
        store.write('settings', {'home_id': 'another-home', 'home_name': 'Another Home', 'homes': []})
        rows = self.accounts('account', store)
        self.assertEqual(rows[0]['title'], 'Work')
        self.assertEqual(rows[0]['subtitle'], 'Current account · 0 homes · Another Home')

    def test_unnamed_connection_without_a_home_uses_a_neutral_name(self):
        store = Store(self.root, keychain_factory=lambda identifier: self.keys)
        row = self.accounts('account', store)[0]
        self.assertEqual(row['title'], 'Aqara account')
        self.assertEqual(row['subtitle'], 'Current account · 0 homes')
        self.assertFalse((self.root / 'accounts.json').exists())

    def test_filter_searches_display_labels_and_preserves_target_ids(self):
        store, _ = self.seed()
        row = self.accounts('account First', store)[0]
        self.assertEqual(row['title'], 'First Home')
        self.assertEqual(json.loads(row['arg'])['profile_id'], 'default')
        self.assertTrue(row['subtitle'].startswith('Switch to this account'))

    def test_available_update_and_learned_ids_do_not_displace_current_account(self):
        store, _ = self.seed()
        self.updater.available.return_value = {'version': '9.0.0'}
        rows = build_items('account', store, self.updater)
        self.assertEqual(rows[0]['title'], 'Work')
        self.assertTrue(rows[0]['subtitle'].startswith('Current account'))
        self.assertEqual(rows[-1]['title'], 'Aqara 9.0.0 is available')
        self.assertTrue(all('uid' not in row for row in rows))

    def test_account_response_preserves_order_in_alfred(self):
        store, _ = self.seed()
        with patch.object(workflow_filter, 'get_store', return_value=store), \
                patch.object(workflow_filter, 'Updater', return_value=self.updater), \
                patch.object(workflow_filter.sys, 'argv', ['filter.py', 'account']):
            output = io.StringIO()
            with redirect_stdout(output):
                workflow_filter.main()
        response = json.loads(output.getvalue())
        self.assertTrue(response['skipknowledge'])
        self.assertEqual(response['items'][0]['title'], 'Work')


if __name__ == '__main__':
    unittest.main()
