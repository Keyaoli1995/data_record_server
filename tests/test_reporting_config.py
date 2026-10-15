import unittest
from data_record_server.config import Config


class ReportingConfigTest(unittest.TestCase):
    def env(self, **changes):
        return dict(WEB_REPORTING_ENABLED='true', WEB_BASE_URL='https://example.test',
                    WEB_ENVIRONMENT='test', WEB_AUTHORIZATION='Bearer secret', **changes)

    def test_disabled_by_default(self):
        self.assertFalse(Config.from_environ({}).reporting.enabled)

    def test_enabled_and_secret_not_in_repr(self):
        value = Config.from_environ(self.env()).reporting
        self.assertTrue(value.enabled)
        self.assertEqual(value.update_interval, 10)
        self.assertNotIn('secret', repr(value))

    def test_required_values_and_unsafe_urls(self):
        for key in ('WEB_BASE_URL', 'WEB_ENVIRONMENT', 'WEB_AUTHORIZATION'):
            env = self.env(); env.pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError):
                Config.from_environ(env)
        for url in ('http://example.test', 'https://user:secret@example.test',
                    'https://example.test/api', 'https://example.test?x=1'):
            env = self.env(); env['WEB_BASE_URL'] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                Config.from_environ(env)

    def test_finite_positive_limits_and_header_injection(self):
        for key in ('WEB_UPDATE_INTERVAL_SECONDS', 'WEB_REQUEST_TIMEOUT_SECONDS'):
            for value in ('0', '-1', 'nan', 'inf'):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    Config.from_environ(self.env(**{key: value}))
        env = self.env(); env['WEB_AUTHORIZATION'] = 'secret\r\nX: bad'
        with self.assertRaises(ValueError):
            Config.from_environ(env)

    def test_explicit_insecure_http_for_local_mock(self):
        env = self.env(WEB_ALLOW_HTTP='true'); env['WEB_BASE_URL'] = 'http://localhost:8000'
        self.assertTrue(Config.from_environ(env).reporting.enabled)
