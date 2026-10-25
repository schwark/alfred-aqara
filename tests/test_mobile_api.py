import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from tools.mobile_api import AppIdentity, CloudAPIError, CloudClient, NoRedirects, encrypt_password, signature


class MobileAPITest(unittest.TestCase):
    def setUp(self):
        # Two known Mersenne primes make a test-only key; no production key fixture.
        p, q = 2 ** 521 - 1, 2 ** 607 - 1
        self.identity = AppIdentity('exampleappid0000', 'example_application_key', p * q)
        self.private_exponent = pow(65537, -1, (p - 1) * (q - 1))
        self.opener = Mock()
        self.client = CloudClient(self.identity, 'US', 'US', self.opener)

    def response(self, data):
        self.opener.open.return_value = io.BytesIO(json.dumps(data).encode())

    def test_password_is_randomized_rsa_pkcs1_encrypted_md5(self):
        ciphertext = base64.b64decode(encrypt_password('example-password', self.identity))
        decoded = pow(int.from_bytes(ciphertext, 'big'), self.private_exponent, self.identity.modulus).to_bytes(len(ciphertext), 'big')
        self.assertTrue(decoded.startswith(b'\0\2'))
        padding, message = decoded[2:].split(b'\0', 1)
        self.assertGreaterEqual(len(padding), 8)
        self.assertEqual(message, hashlib.md5(b'example-password').hexdigest().encode())
        self.assertNotEqual(base64.b64encode(ciphertext).decode(), encrypt_password('example-password', self.identity))

    def test_signing_distinguishes_empty_token_and_raw_get_parameters(self):
        raw = 'Appid=exampleappid0000&Nonce=nonce&Time=123&x=["value"]&example_application_key'
        self.assertEqual(signature(self.identity, 'nonce', '123', '', 'x=["value"]'), hashlib.md5(raw.encode()).hexdigest())
        self.assertNotEqual(signature(self.identity, 'nonce', '123', '', ''), signature(self.identity, 'nonce', '123', 'token', ''))

    def test_reference_parser_does_not_execute_python(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'const.py'
            path.write_text("raise RuntimeError('must never execute')\nAPPID='exampleappid0000'\nAPPKEY='example_application_key'\nRSA_N=int('" + format(self.identity.modulus, 'x') + "',16)\nRSA_E=65537\n")
            self.assertEqual(AppIdentity.from_reference(path).modulus, self.identity.modulus)
            path.write_text("APPID=__import__('os').getenv('HOME')")
            with self.assertRaises(CloudAPIError):
                AppIdentity.from_reference(path)

    def test_sign_in_keeps_password_out_of_headers_and_diagnostics(self):
        self.response({'code': 0, 'result': {'token': 'example-token', 'userInfo': {'userId': 'example-user'}}})
        self.client.login('example@example.invalid', 'example-password')
        request = self.opener.open.call_args[0][0]
        self.assertTrue(request.full_url.startswith('https://aiot-rpc-usa.aqara.com/app/v1.0/lumi/'))
        self.assertEqual(self.client.token, 'example-token')
        self.assertNotIn(b'example-password', request.data)
        self.assertNotIn('example-password', str(request.header_items()))
        self.assertEqual(json.loads(request.data)['district'], 'US')
        self.response({'code': 810, 'message': 'private-account-or-password'})
        with self.assertRaises(CloudAPIError) as caught:
            self.client.login('example@example.invalid', 'example-password')
        self.assertEqual(caught.exception.code, 810)
        self.assertNotIn('private-account-or-password', str(caught.exception))
        self.assertEqual(self.client.token, '')

    def test_signed_get_encodes_url_but_signs_raw_query(self):
        self.client.token, self.client.user_id = 'example-token', 'example-user'
        self.response({'code': 0, 'result': {'devices': []}})
        with patch('tools.mobile_api.secrets.token_hex', return_value='a' * 32), patch('tools.mobile_api.time.time', return_value=123):
            self.client.devices('example home')
        request = self.opener.open.call_args[0][0]
        self.assertIn('positionId=example+home', request.full_url)
        expected = signature(self.identity, 'A' * 32, '123000', 'example-token', 'positionId=example home&size=300&startIndex=0')
        self.assertEqual(request.get_header('Sign'), expected)

    def test_client_cannot_send_control_writes_or_fetch_lock_credentials(self):
        self.client.token = 'example-token'
        for method, path in [('POST', '/matter/write'), ('GET', '/dev/lock/query'),
                             ('POST', '/dev/lock/user/add'), ('POST', '/devex/lock/pwd')]:
            with self.subTest(path=path), self.assertRaises(CloudAPIError):
                self.client._call(method, path, {})
        self.opener.open.assert_not_called()
        with self.assertRaises(CloudAPIError):
            CloudClient(self.identity, 'https://example.invalid', 'US')

    def test_get_query_signing_is_canonical_regardless_of_caller_field_order(self):
        self.client.token = 'example-token'
        self.response({'code': 0, 'result': []})
        with patch('tools.mobile_api.secrets.token_hex', return_value='a' * 32), patch('tools.mobile_api.time.time', return_value=123):
            self.client._call('GET', '/app/position/device/query', {'startIndex': 0, 'size': 300, 'positionId': 'example home'})
        request = self.opener.open.call_args[0][0]
        self.assertTrue(request.full_url.endswith('?positionId=example+home&size=300&startIndex=0'))
        self.assertEqual(request.get_header('Sign'), signature(self.identity, 'A' * 32, '123000', 'example-token',
                         'positionId=example home&size=300&startIndex=0'))

    def test_matter_queries_use_device_endpoint_metadata_and_read_route(self):
        self.client.token = 'example-token'
        self.response({'code': 0, 'result': {'endpoints': []}})
        self.client.matter_details('example-device')
        request = self.opener.open.call_args[0][0]
        self.assertTrue(request.full_url.endswith('/matter/dev/detail?did=example-device&fullValue=true'))
        self.assertEqual(request.method, 'GET')
        self.response({'code': 0, 'result': []})
        self.client.matter_status('example-device', ['7.148.32970.1'])
        request = self.opener.open.call_args[0][0]
        self.assertTrue(request.full_url.endswith('/res/query/by/resourceId'))
        self.assertEqual(json.loads(request.data), {'data': [{'subjectId': 'example-device', 'options': ['7.148.32970.1']}]})

    def test_zigbee_panels_and_traits_are_read_only_without_subscription(self):
        self.client.token = 'example-token'
        self.response({'code': 0, 'result': []})
        self.client.panels('example-device')
        request = self.opener.open.call_args[0][0]
        self.assertTrue(request.full_url.endswith('/app/layout/collection/panels?subjectIds=example-device&types=device_endpoint_panel'))
        self.response({'code': 0, 'result': []})
        self.client.traits('example-device', ['2.148.32970', '2.148.33026'])
        request = self.opener.open.call_args[0][0]
        self.assertTrue(request.full_url.endswith('/app/qlink/trait/read'))
        self.assertEqual(json.loads(request.data), {'devices': [{'deviceId': 'example-device', 'traits': [
            {'path': '2.148.32970', 'needSubscribe': False}, {'path': '2.148.33026', 'needSubscribe': False}]}], 'needParam': True})
        for paths in ([], ['2.148.32970.1'], ['arbitrary']):
            with self.assertRaises(CloudAPIError):
                self.client.traits('example-device', paths)

    def test_no_session_prevents_network_and_missing_user_id_rejects_session(self):
        with self.assertRaises(CloudAPIError):
            self.client.homes()
        self.opener.open.assert_not_called()
        for user_info in (None, 'unexpected'):
            self.response({'code': 0, 'result': {'token': 'example-token', 'userInfo': user_info}})
            with self.assertRaises(CloudAPIError):
                self.client.login('example@example.invalid', 'example-password')
            self.assertEqual(self.client.token, '')

    def test_http_errors_timeouts_bad_json_and_oversized_bodies_are_safe(self):
        self.client.token = 'example-token'
        for error in [HTTPError('https://example.invalid', 302, 'private', {}, io.BytesIO(b'private')), URLError('private')]:
            self.opener.open.side_effect = error
            with self.subTest(error=type(error)), self.assertRaises(CloudAPIError) as caught:
                self.client.homes()
            self.assertNotIn('private', str(caught.exception))
        self.opener.open.side_effect = None
        for raw in [b'not JSON private', b'x' * (4 * 1024 * 1024 + 1), b'[]', b'{}']:
            self.opener.open.return_value = io.BytesIO(raw)
            with self.subTest(size=len(raw)), self.assertRaises(CloudAPIError):
                self.client.homes()

    def test_redirects_do_not_forward_a_session(self):
        self.assertIsNone(NoRedirects().redirect_request(None, None, 302, '', {}, 'https://example.invalid'))


if __name__ == '__main__':
    unittest.main()
