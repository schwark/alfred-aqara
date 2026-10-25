import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import command
import filter as workflow_filter
from common import AqaraError, Store
from filter import build_items


class RefreshTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name, keychain_factory=lambda identifier: Mock())
        self.store.workflow_root = self.store.root
        self.store.write('settings', {'home_id': 'example-home', 'home_name': 'Example Home'})
        self.store.write('catalog', {'home_id': 'example-home', 'devices': [
            {'id': 'example-sensor', 'name': 'Desk Sensor', 'type': 'OccupancySensor'}], 'scenes': []})
        self.updater = Mock()
        self.updater.available.return_value = None
        self.updater.checking.return_value = False

    def action(self):
        return json.loads(build_items('upd', self.store, self.updater)[0]['arg'])

    def test_refresh_shows_progress_before_requests_and_completion_after_catalog_update(self):
        client = Mock()
        client.request.side_effect = [
            [{'positionId': 'example-home', 'positionName': 'Example Home'}],
            [{'endpoint_id': 'example-light', 'device_name': 'Desk Light', 'device_type': 'Light'}], []]
        action = self.action()
        def started():
            client.request.assert_not_called()
            row = build_items('upd', self.store, self.updater)[0]
            self.assertEqual(row['title'], 'Refreshing devices and scenes…')
            self.assertFalse(row['valid'])
        with patch.object(command, 'notify_refresh_started', side_effect=started), \
                patch.object(self.store, 'client', return_value=client):
            message = command.execute(action, self.store)
        self.assertIn('refreshed 1 devices and 0 scenes', message)
        self.assertEqual(self.store.read('catalog')['devices'][0]['id'], 'example-light')
        row = build_items('upd', self.store, self.updater)[0]
        self.assertTrue(row['valid'])
        self.assertIn('Last refreshed just now', row['subtitle'])
        self.assertIn('1 devices · 0 scenes', row['subtitle'])

    def test_refresh_remains_first_with_available_upgrade_and_unique_device_match(self):
        self.updater.available.return_value = {'version': '1.0.0'}
        for query in ('up', 'upd', 'update'):
            rows = build_items(query, self.store, self.updater)
            self.assertEqual(json.loads(rows[0]['arg'])['action'], 'update')
            self.assertEqual(rows[1]['title'], 'Aqara 1.0.0 is available')
            self.assertTrue(all('uid' not in row for row in rows))
            self.assertEqual(sum(json.loads(row.get('arg', '{}')).get('action') == 'workflow_update' for row in rows), 1)
        self.assertEqual(build_items('workflow:u', self.store, self.updater)[0]['title'], 'Aqara 1.0.0 is available')

    def test_running_refresh_reruns_filter_without_learned_sorting(self):
        self.store.write('refresh', {'state': 'running', 'pid': os.getpid()})
        with patch.object(workflow_filter, 'get_store', return_value=self.store), \
                patch.object(workflow_filter, 'Updater', return_value=self.updater), \
                patch.object(workflow_filter.sys, 'argv', ['filter.py', 'upd']):
            output = io.StringIO()
            with redirect_stdout(output):
                workflow_filter.main()
        response = json.loads(output.getvalue())
        self.assertTrue(response['skipknowledge'])
        self.assertEqual(response['rerun'], 0.5)
        self.assertFalse(response['items'][0]['valid'])

    def test_failure_keeps_catalog_and_offers_retry(self):
        previous = self.store.read('catalog')
        with patch.object(command, 'notify_refresh_started'), \
                patch.object(self.store, 'refresh', side_effect=AqaraError('Cloud session expired')):
            with self.assertRaisesRegex(AqaraError, 'expired'):
                command.execute(self.action(), self.store)
        self.assertEqual(self.store.read('catalog'), previous)
        self.assertEqual(self.store.read('refresh')['state'], 'failed')
        row = build_items('upd', self.store, self.updater)[0]
        self.assertTrue(row['valid'])
        self.assertIn('Last refresh failed', row['subtitle'])

    def test_running_refresh_is_not_duplicated_and_interrupted_refresh_can_retry(self):
        action = self.action()
        self.store.write('refresh', {'state': 'running', 'pid': os.getpid()})
        with patch.object(command, 'notify_refresh_started') as notify, patch.object(self.store, 'refresh') as refresh:
            self.assertIn('Already refreshing', command.execute(action, self.store))
        refresh.assert_not_called()
        notify.assert_not_called()
        with patch('common.os.kill', side_effect=ProcessLookupError):
            row = build_items('upd', self.store, self.updater)[0]
        self.assertTrue(row['valid'])
        self.assertIn('interrupted', row['subtitle'])

    def test_changed_home_rejects_stale_refresh_and_hides_other_home_counts(self):
        action = self.action()
        self.store.write('refresh', {'state': 'complete', 'home_id': 'example-home', 'finished': 100, 'devices': 3, 'scenes': 1})
        self.store.write('settings', {'home_id': 'different-home'})
        with patch.object(self.store, 'refresh') as refresh, patch.object(command, 'notify_refresh_started') as notify:
            with self.assertRaisesRegex(AqaraError, 'changed'):
                command.execute(action, self.store)
        refresh.assert_not_called()
        notify.assert_not_called()
        self.assertNotIn('Last refreshed', build_items('upd', self.store, self.updater)[0]['subtitle'])

    def test_notification_failure_does_not_prevent_refresh(self):
        with patch.object(command.subprocess, 'run', side_effect=subprocess.TimeoutExpired('notification', 2)), \
                patch.object(self.store, 'refresh', return_value=(2, 0)):
            self.assertIn('refreshed 2 devices', command.execute(self.action(), self.store))
        self.assertEqual(self.store.read('refresh')['state'], 'complete')

    def test_clearing_catalog_removes_refresh_status(self):
        self.store.write('refresh', {'state': 'failed'})
        self.store.clear_catalog()
        self.assertEqual(self.store.read('refresh'), {})


if __name__ == '__main__':
    unittest.main()
