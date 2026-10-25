import hashlib
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request
from zipfile import ZipFile

import command
import filter as workflow_filter
from common import AqaraError, BUNDLE_ID, Store
from filter import build_items
from updater import GitHubRedirects, REPOSITORY, UPDATE_KEY, Updater, verified_download
from workflow import background, update
from workflow.update import Download


def release(version, *, draft=False, prerelease=False, extension='alfredworkflow'):
    filename = 'Aqara-' + version + '.' + extension
    return {'tag_name': 'v' + version, 'draft': draft, 'prerelease': prerelease, 'assets': [{
        'browser_download_url': 'https://github.com/' + REPOSITORY + '/releases/download/v' + version + '/' + filename,
        'digest': 'sha256:' + 'a' * 64, 'size': 1234,
    }]}


def package(version='1.0.0', bundle_id=BUNDLE_ID, extra=None):
    stream = io.BytesIO()
    with ZipFile(stream, 'w') as archive:
        archive.writestr('info.plist', plistlib.dumps({'bundleid': bundle_id, 'version': version}))
        archive.writestr('version', version)
        if extra:
            archive.writestr(extra, 'test')
    return stream.getvalue()


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.keys = Mock()
        self.store = Store(self.temp.name, keychain_factory=lambda identifier: self.keys)
        self.store.write('settings', {'home_id': 'example-home', 'home_name': 'Example Home'})
        self.updater = Updater(self.store.root, '0.2.6')
        self.updater._bind()

    def cache_update(self, version='1.0.0'):
        dl = Download.from_releases(json.dumps([release(version)]))[0]
        self.updater.workflow.cache_data(UPDATE_KEY, {'available': True, 'version': version, 'download': dl.dict})

    def test_framework_selects_stable_compatible_release_and_keeps_integrity_fields(self):
        payload = [release('1.0.0'), release('2.0.0', prerelease=True), release('3.0.0', draft=True),
                   release('1.5.0', extension='alfred6workflow')]
        downloads = Download.from_releases(json.dumps(payload))
        self.assertNotIn('3.0.0', [str(dl.version) for dl in downloads])
        chosen = update.latest_download(downloads, '5.8.1')
        self.assertEqual(str(chosen.version), '1.0.0')
        roundtrip = Download.from_dict(chosen.dict)
        self.assertEqual(roundtrip.digest, 'sha256:' + 'a' * 64)
        self.assertEqual(roundtrip.size, 1234)

    def test_framework_rejects_ambiguous_workflow_assets(self):
        payload = release('1.0.0')
        payload['assets'].append(payload['assets'][0].copy())
        self.assertEqual(Download.from_releases(json.dumps([payload])), [])

    def test_daily_background_checks_use_framework_and_hourly_failure_backoff(self):
        with patch.object(background, 'run_in_background') as spawn:
            self.updater.schedule()
            self.updater.schedule()
        spawn.assert_called_once_with('__workflow_update_check', [sys.executable, '-m', 'workflow.update', 'check', REPOSITORY, '0.2.6'])
        self.updater.workflow.settings['__aqara_next_check'] = 0
        self.cache_update()
        with patch.object(background, 'run_in_background') as spawn:
            self.updater.schedule()
        spawn.assert_not_called()
        path = self.updater.workflow.cachefile(UPDATE_KEY + '.pickle')
        os.utime(path, (1, 1))
        with patch.object(background, 'run_in_background') as spawn:
            self.updater.schedule()
        spawn.assert_called_once()

    def test_disabled_checks_still_allow_manual_updates(self):
        self.updater.set_enabled(False)
        with patch.object(background, 'run_in_background') as spawn, patch.object(update, 'get_downloads', return_value=[]):
            self.updater.schedule()
            self.assertIn('up to date', self.updater.install())
        spawn.assert_not_called()
        self.assertFalse(Updater(self.store.root, '0.2.6').enabled())

    def test_upgrade_checks_again_despite_previous_daily_cache_and_backoff(self):
        self.updater.workflow.cache_data(UPDATE_KEY, {'available': False})
        release_key = 'github-releases-' + REPOSITORY.replace('/', '-')
        self.updater.workflow.cache_data(release_key, b'old release list')
        self.updater.workflow.settings['__aqara_check_version'] = '0.2.5'
        self.updater.workflow.settings['__aqara_next_check'] = 10**12
        with patch.object(background, 'run_in_background') as spawn:
            self.updater.schedule()
            self.updater.schedule()
        spawn.assert_called_once()
        self.assertIsNone(self.updater.workflow.cached_data(UPDATE_KEY, max_age=0))
        self.assertIsNone(self.updater.workflow.cached_data(release_key, max_age=0))
        self.assertEqual(self.updater.workflow.settings['__aqara_check_version'], '0.2.6')

    def test_disabled_checks_keep_existing_cache_after_upgrade(self):
        self.updater.set_enabled(False)
        self.cache_update()
        with patch.object(background, 'run_in_background') as spawn:
            self.updater.schedule()
        spawn.assert_not_called()
        self.assertEqual(self.updater.available()['version'], '1.0.0')

    def test_background_runner_inherits_isolated_directories(self):
        with patch.object(background, 'is_running', return_value=False), patch.object(background.subprocess, 'run') as run:
            run.return_value.returncode = 0
            background.run_in_background('example-job', ['example-command'])
        args, kwargs = run.call_args
        self.assertEqual(args[0], [sys.executable, '-m', 'workflow.background', 'example-job'])
        self.assertEqual(kwargs['env']['alfred_workflow_data'], self.updater.workflow.datadir)
        self.assertEqual(kwargs['env']['alfred_workflow_cache'], self.updater.workflow.cachedir)

    def test_framework_settings_do_not_touch_accounts_or_credentials(self):
        original = (self.store.root / 'settings.json').read_bytes()
        self.updater.set_enabled(False)
        self.cache_update()
        self.assertEqual((self.store.root / 'settings.json').read_bytes(), original)
        self.assertEqual(Path(self.updater.workflow.settings_path).parent, self.store.root / 'workflow-updates' / 'data')
        self.assertEqual(self.updater.directory.stat().st_mode & 0o777, 0o700)
        self.keys.get.assert_not_called()

    def test_new_release_row_is_actionable_and_disappears_after_upgrade(self):
        self.cache_update()
        row = build_items('home', self.store, self.updater)[0]
        self.assertEqual(row['title'], 'Aqara 1.0.0 is available')
        self.assertEqual(row['autocomplete'], 'workflow:update')
        self.assertEqual(json.loads(row['arg']), {'action': 'workflow_update', 'mode': 'agent', 'mode_revision': ''})
        self.assertIsNone(Updater(self.store.root, '1.0.0').available())
        self.assertIsNone(Updater(self.store.root, '2.0.0').available())

    def test_workflow_controls_and_upgrade_alias_do_not_refresh_devices(self):
        for query in ('workflow:update', 'upgrade'):
            self.assertEqual(json.loads(build_items(query, self.store)[0]['arg']), {'action': 'workflow_update', 'mode': 'agent', 'mode_revision': ''})
        self.assertEqual(len(build_items('workflow:', self.store)), 3)
        with patch.object(command, 'Updater') as updater, patch.object(self.store, 'refresh') as refresh:
            updater.return_value.install.return_value = 'Downloading update.'
            self.assertEqual(command.execute({'action': 'workflow_update'}, self.store), 'Downloading update.')
            command.execute({'action': 'workflow_autoupdate', 'enabled': False}, self.store)
            updater.return_value.set_enabled.assert_called_once_with(False)
            refresh.assert_not_called()
        self.keys.get.assert_not_called()

    def test_workflow_commands_complete_from_partial_namespace(self):
        for query in ('w', 'work', 'workflow', 'WORKFLOW:', 'workflow:u', 'workflow:update'):
            with self.subTest(query=query):
                row = build_items(query, self.store, self.updater)[0]
                self.assertEqual(row['autocomplete'], 'workflow:update')
                self.assertEqual(json.loads(row['arg'])['action'], 'workflow_update')
        self.store.write('catalog', {'home_id': 'example-home', 'devices': [
            {'id': 'example-light', 'name': 'Work Light', 'type': 'Light'}], 'scenes': []})
        rows = build_items('work', self.store, self.updater)
        self.assertTrue(any(row['autocomplete'] == 'workflow:update' for row in rows))
        self.assertTrue(any(row.get('autocomplete', '').startswith('Work Light') for row in rows))

    def test_short_update_search_offers_refresh_and_upgrade_without_changing_update(self):
        for query in ('up', 'upd', 'update'):
            with self.subTest(query=query):
                rows = build_items(query, self.store, self.updater)
                actions = [json.loads(row['arg'])['action'] for row in rows if 'arg' in row]
                self.assertEqual(actions, ['update', 'workflow_update'])
                self.assertEqual(rows[1]['autocomplete'], 'workflow:update')
        for query, enabled in (('auto', True), ('noauto', False)):
            row = build_items(query, self.store, self.updater)[0]
            self.assertEqual(json.loads(row['arg'])['enabled'], enabled)

    def test_filter_refreshes_while_background_check_runs_then_shows_release(self):
        self.store.workflow_root = self.store.root
        with patch.object(workflow_filter, 'get_store', return_value=self.store), \
                patch.object(workflow_filter, 'Updater', return_value=self.updater), \
                patch.object(self.updater, 'schedule'), \
                patch.object(background, 'is_running', side_effect=[True, False]), \
                patch.object(sys, 'argv', ['filter.py', 'workflow:u']):
            output = io.StringIO()
            with redirect_stdout(output):
                workflow_filter.main()
            self.assertEqual(json.loads(output.getvalue())['rerun'], 0.5)
            self.cache_update()
            output = io.StringIO()
            with redirect_stdout(output):
                workflow_filter.main()
            response = json.loads(output.getvalue())
            self.assertNotIn('rerun', response)
            self.assertEqual(response['items'][0]['title'], 'Aqara 1.0.0 is available')

    def test_install_uses_framework_download_job_and_hides_failures(self):
        downloads = Download.from_releases(json.dumps([release('1.0.0')]))
        with patch.object(update, 'get_downloads', return_value=downloads), patch.object(background, 'run_in_background') as spawn:
            self.assertIn('Confirm the upgrade in Alfred', self.updater.install())
        spawn.assert_called_once_with('__workflow_update_install', [sys.executable, '-m', 'workflow.update', 'install', REPOSITORY, '0.2.6'])
        with patch.object(update, 'get_downloads', side_effect=OSError('example private diagnostic')):
            with self.assertRaises(AqaraError) as caught:
                self.updater.install()
        self.assertNotIn('private diagnostic', str(caught.exception))

    def test_manual_update_discards_cached_release_list(self):
        release_key = 'github-releases-' + REPOSITORY.replace('/', '-')
        self.updater.workflow.cache_data(release_key, b'old release list')
        def check(*args):
            self.assertIsNone(self.updater.workflow.cached_data(release_key, max_age=0))
            return False
        with patch.object(update, 'check_update', side_effect=check):
            self.assertIn('up to date', self.updater.install())

    def test_check_progress_failure_does_not_break_search(self):
        with patch.object(background, 'is_running', side_effect=OSError('unavailable')):
            self.assertFalse(self.updater.checking())

    def test_background_failure_and_corrupt_cache_do_not_break_search(self):
        with patch.object(self.updater.workflow, 'check_update', side_effect=subprocess.SubprocessError()):
            self.updater.schedule()
        Path(self.updater.workflow.cachefile(UPDATE_KEY + '.pickle')).write_bytes(b'not a pickle')
        self.assertIsNone(self.updater.available())
        self.assertTrue(build_items('home', self.store, self.updater))


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = package()
        self.download = Download.from_releases(json.dumps([release('1.0.0')]))[0]
        self.set_data(self.data)

    def set_data(self, data):
        self.download.size = len(data)
        self.download.digest = 'sha256:' + hashlib.sha256(data).hexdigest()
        self.opener = Mock()
        self.opener.open.return_value = io.BytesIO(data)

    def fetch(self):
        return verified_download(self.download, self.temp.name, self.opener)

    def assert_no_downloads(self):
        self.assertEqual(list((Path(self.temp.name) / 'downloads').glob('*')), [])

    def test_verified_download_uses_public_request_and_private_local_file(self):
        output = self.fetch()
        self.assertEqual(output.read_bytes(), self.data)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(output.name, 'Aqara-1.0.0.alfredworkflow')
        request = self.opener.open.call_args[0][0]
        self.assertEqual({name.lower() for name, _ in request.header_items()}, {'user-agent', 'accept'})

    def test_bad_checksum_and_wrong_download_size_leave_no_partial_file(self):
        for size, digest in ((len(self.data), 'sha256:' + '0' * 64), (len(self.data) - 1, self.download.digest), (len(self.data) + 1, self.download.digest)):
            with self.subTest(size=size), self.assertRaises(AqaraError):
                self.set_data(self.data)
                self.download.size, self.download.digest = size, digest
                self.fetch()
            self.assert_no_downloads()

    def test_rejects_wrong_bundle_version_archive_paths_and_invalid_zip(self):
        for data in (package(bundle_id='example.other'), package(version='2.0.0'),
                     package(extra='../outside'), package(extra='/outside'), package(extra='folder\\outside'), b'not a zip'):
            with self.subTest(data_size=len(data)), self.assertRaises(AqaraError):
                self.set_data(data)
                self.fetch()
            self.assert_no_downloads()

    def test_repository_url_asset_and_checksum_are_required_before_network(self):
        original = self.download.dict
        cases = [('url', self.download.url.replace(REPOSITORY, 'example/other')),
                 ('url', self.download.url.replace('https:', 'http:')),
                 ('url', self.download.url + '?token=example'), ('filename', '../Aqara-1.0.0.alfredworkflow'),
                 ('digest', None), ('size', 0), ('size', 32 * 1024 * 1024)]
        for attribute, value in cases:
            self.download = Download.from_dict(original)
            setattr(self.download, attribute, value)
            with self.subTest(attribute=attribute), self.assertRaises(AqaraError):
                self.fetch()
        self.opener.open.assert_not_called()

    def test_redirects_allow_github_asset_host_and_reject_other_hosts(self):
        handler = GitHubRedirects()
        request = Request('https://github.com/example/download')
        self.assertEqual(handler.redirect_request(request, None, 302, '', {}, 'https://release-assets.githubusercontent.com/example').host,
                         'release-assets.githubusercontent.com')
        for url in ('http://github.com/example', 'https://example.com/asset', 'https://github.com.example/asset'):
            with self.subTest(url=url), self.assertRaises(AqaraError):
                handler.redirect_request(request, None, 302, '', {}, url)

    def test_framework_imports_only_after_verified_download(self):
        wf = Updater(self.temp.name, '0.2.6')
        wf._bind()
        status = {'available': True, 'version': '1.0.0', 'download': self.download.dict}
        wf.workflow.cache_data(UPDATE_KEY, status)
        output = Path(self.temp.name) / self.download.filename
        with patch('updater.verified_download', return_value=output) as download, patch.object(update.subprocess, 'run') as open_file:
            self.assertTrue(update.install_update())
        download.assert_called_once()
        open_file.assert_called_once_with(['/usr/bin/open', str(output)], check=True)
        self.assertIsNone(wf.available())
        wf.workflow.cache_data(UPDATE_KEY, status)
        with patch('updater.verified_download', side_effect=AqaraError('Integrity check failed')), patch.object(update.subprocess, 'run') as open_file:
            with self.assertRaises(AqaraError):
                update.install_update()
        open_file.assert_not_called()


if __name__ == '__main__':
    unittest.main()
