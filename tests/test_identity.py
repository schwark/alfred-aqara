import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from cloud_backend import CloudStore, load_identity, save_identity
from cloud_transport import AppIdentity, CloudAPIError, NoRedirects
from common import AqaraError
from identity_fixture import reference


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reference = reference(self.root)
        self.raw = self.reference.read_bytes()
        self.identity = AppIdentity.from_reference(self.reference)
        self.cached = None
        self.cache = Mock()
        self.cache.get.side_effect = lambda: self.cached
        self.cache.set.side_effect = lambda value: setattr(self, "cached", value)
        self.override = Mock()
        self.override.get.return_value = None
        self.opener = Mock()
        self.opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(self.raw)
        for context in (patch("cloud_backend.identity_key", return_value=self.override),
                        patch("cloud_backend.public_identity_key", return_value=self.cache),
                        patch("cloud_identity.REFERENCE_SHA256", hashlib.sha256(self.raw).hexdigest()),
                        patch("cloud_identity.build_opener", return_value=self.opener)):
            context.start()
            self.addCleanup(context.stop)

    def test_fresh_machine_downloads_and_caches_without_manual_import(self):
        self.assertEqual(load_identity(), self.identity)
        self.opener.open.assert_called_once()
        self.cache.set.assert_called_once()
        self.override.set.assert_not_called()
        self.assertEqual(json.loads(self.cached)["identity"], self.identity.payload())

    def test_cached_values_work_without_github_on_later_uses(self):
        load_identity()
        self.opener.reset_mock()
        self.opener.open.side_effect = URLError("unavailable")
        self.assertEqual(load_identity(), self.identity)
        self.opener.open.assert_not_called()

    def test_existing_manual_override_is_preserved_without_download(self):
        self.override.get.return_value = json.dumps(dict(self.identity.payload(), app_id="customexampleappid"))
        self.assertEqual(load_identity().app_id, "customexampleappid")
        self.opener.open.assert_not_called()
        self.cache.set.assert_not_called()

    def test_corrupt_or_outdated_cache_is_recovered_automatically(self):
        for cached in ("invalid JSON", "[]", json.dumps({"reference_sha256":"old-reference"}),
                       json.dumps({"reference_sha256":hashlib.sha256(self.raw).hexdigest(),"identity":{"token":"private-test-value"}})):
            self.cached = cached
            self.assertEqual(load_identity(), self.identity)
        self.assertEqual(self.opener.open.call_count, 4)

    def test_login_only_prompts_for_account_details_on_a_fresh_machine(self):
        key = Mock(); key.get.return_value = "{}"
        store = CloudStore(self.root / "data", keychain_factory=lambda identifier: key)
        with patch("cloud_backend.dialog", side_effect=["US", "US", "example@example.invalid", "example-password"]) as dialogs, patch("cloud_backend.BackendClient") as client, patch.object(store, "replace_connection"):
            message = store.connect()
        self.assertIn("Cloud session saved", message)
        self.assertEqual(dialogs.call_count, 4)
        client.assert_called_once_with(self.identity, "US", "US")
        client.return_value.login.assert_called_once_with("example@example.invalid", "example-password")

    def test_public_request_has_no_account_or_app_credentials(self):
        import cloud_identity
        load_identity()
        self.assertIsInstance(cloud_identity.build_opener.call_args.args[0], NoRedirects)
        request = self.opener.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith("https://raw.githubusercontent.com/"))
        self.assertIn("4fc274688cd40774307549172c9ae1db073c92ca", request.full_url)
        self.assertIsNone(request.data)
        self.assertEqual({name.casefold() for name, _ in request.header_items()}, {"user-agent"})

    def test_changed_or_oversized_reference_is_rejected_without_caching(self):
        for raw in (b"private-unexpected-response", b"x" * 65537):
            self.opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(raw)
            with self.assertRaises(CloudAPIError) as raised:
                load_identity()
            self.assertNotIn("private-unexpected-response", str(raised.exception))
        self.cache.set.assert_not_called()

    def test_network_failures_request_retry_without_disclosing_response(self):
        errors = [URLError("private-network-detail"), HTTPError("https://example.invalid",403,"private-body",{},io.BytesIO(b"private-body"))]
        for error in errors:
            self.opener.open.side_effect = error
            with self.assertRaises(CloudAPIError) as raised:
                load_identity()
            self.assertIn("retry aq login", str(raised.exception))
            self.assertNotIn("private-", str(raised.exception))
        self.cache.set.assert_not_called()

    def test_reference_is_parsed_as_data_and_never_executed(self):
        self.raw = b"raise RuntimeError('must not execute')\n" + self.raw
        with patch("cloud_identity.REFERENCE_SHA256", hashlib.sha256(self.raw).hexdigest()):
            self.assertEqual(load_identity(), self.identity)

    def test_personal_fields_are_not_accepted_in_identity_payload(self):
        for field in ("token", "password", "user_id", "agent_key", "homes"):
            with self.subTest(field=field), self.assertRaises(CloudAPIError):
                AppIdentity.from_payload(dict(self.identity.payload(), **{field:"private-test-value"}))

    def test_advanced_reference_and_json_overrides_remain_supported(self):
        save_identity(self.reference)
        self.assertEqual(json.loads(self.override.set.call_args.args[0]), self.identity.payload())
        path=self.root / "custom.json"; path.write_text(json.dumps(self.identity.payload()))
        save_identity(path)
        self.assertEqual(json.loads(self.override.set.call_args.args[0]), self.identity.payload())

    def test_invalid_manual_override_has_a_safe_specific_error(self):
        self.override.get.return_value = '{"token":"private-test-value"}'
        with self.assertRaises(AqaraError) as raised:
            load_identity()
        self.assertIn("advanced Cloud signing override", str(raised.exception))
        self.assertNotIn("private-test-value", str(raised.exception))
        self.opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
