import unittest

from tools.mobile_capture_guard import permitted


class CaptureGuardTest(unittest.TestCase):
    def test_allows_login_and_known_reads(self):
        for suffix in ("/user/guard-code/login", "/res/query", "/res/query/by/resourceId",
                       "/devex/device/pwd/state/query", "/app/qlink/trait/read"):
            self.assertTrue(permitted("POST", "aiot-rpc-usa.aqara.com", "/app/v1.0/lumi" + suffix))
        self.assertTrue(permitted("GET", "rpc-au.aqara.com", "/app/v1.0/lumi/matter/dev/detail?did=example"))

    def test_rejects_controls_and_password_changes_for_all_verbs(self):
        for verb in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            for suffix in ("/matter/write", "/res/write", "/app/qlink/trait/write",
                           "/devex/lock/pwd"):
                with self.subTest(verb=verb, suffix=suffix):
                    self.assertFalse(permitted(verb, "rpc-au.aqara.com", "/app/v1.0/lumi" + suffix))

    def test_unknown_posts_and_lookalike_read_paths_are_blocked(self):
        for path in ("/app/v1.0/lumi/dev/lock/user/add", "/app/v1.0/lumi/res/query/extra",
                     "/unrelated/path", "/app/v1.0/lumi/%6datter/write"):
            self.assertFalse(permitted("POST", "aiot-rpc.ankasa.cn", path))

    def test_domain_checks_handle_case_and_trailing_dot(self):
        self.assertFalse(permitted("POST", "RPC-AU.AQARA.COM.", "/unknown"))
        self.assertTrue(permitted("GET", "cdn.aqara.com", "/static/plugin.zip"))

    def test_does_not_intercept_other_services(self):
        self.assertTrue(permitted("POST", "example.invalid", "/unknown"))


if __name__ == "__main__":
    unittest.main()
