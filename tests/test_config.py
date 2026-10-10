import os
import unittest
from pathlib import Path
from unittest.mock import patch

from data_record_server.config import Config


class ConfigTest(unittest.TestCase):
    def test_uses_documented_defaults_when_environment_is_empty(self):
        with patch.dict(os.environ, {}, clear=True):
            config = Config.from_environ()

        self.assertEqual("0.0.0.0", config.host)
        self.assertEqual(30050, config.port)
        self.assertEqual(Path("/data"), config.data_dir)
        self.assertEqual(4096, config.read_buffer_bytes)

    def test_uses_a_30_second_idle_timeout_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            config = Config.from_environ()

        self.assertEqual(30, config.idle_timeout_seconds)

    def test_uses_a_64_kib_json_line_limit_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            config = Config.from_environ()

        self.assertEqual(65536, config.max_json_line_bytes)

    def test_uses_a_one_year_device_retention_period_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            config = Config.from_environ()

        self.assertEqual(365, config.device_retention_days)

    def test_uses_a_30_day_connection_retention_period_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            config = Config.from_environ()

        self.assertEqual(30, config.connection_retention_days)

    def test_uses_device_retention_days_environment_override(self):
        config = Config.from_environ({"DEVICE_RETENTION_DAYS": "730"})

        self.assertEqual(730, config.device_retention_days)

    def test_rejects_non_positive_device_retention_days(self):
        for value in ("0", "-1", "not-a-number"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "DEVICE_RETENTION_DAYS"):
                    Config.from_environ({"DEVICE_RETENTION_DAYS": value})

    def test_uses_connection_retention_days_environment_override(self):
        config = Config.from_environ({"RETENTION_DAYS": "45"})

        self.assertEqual(45, config.connection_retention_days)

    def test_rejects_non_positive_connection_retention_days(self):
        for value in ("0", "-1", "not-a-number"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "RETENTION_DAYS"):
                    Config.from_environ({"RETENTION_DAYS": value})

    def test_connection_retention_does_not_affect_device_retention(self):
        config = Config.from_environ({"RETENTION_DAYS": "7"})

        self.assertEqual(7, config.connection_retention_days)
        self.assertEqual(365, config.device_retention_days)

    def test_uses_idle_timeout_environment_override(self):
        config = Config.from_environ({"IDLE_TIMEOUT_SECONDS": "45"})

        self.assertEqual(45, config.idle_timeout_seconds)

    def test_rejects_invalid_idle_timeout_values(self):
        for value in ("0", "-1", "not-a-number"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "IDLE_TIMEOUT_SECONDS"):
                    Config.from_environ({"IDLE_TIMEOUT_SECONDS": value})

    def test_rejects_non_positive_json_line_limit(self):
        for value in ("0", "-1", "not-a-number"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "MAX_JSON_LINE_BYTES"):
                    Config.from_environ({"MAX_JSON_LINE_BYTES": value})

    def test_uses_environment_overrides(self):
        environment = {
            "TCP_HOST": "127.0.0.1",
            "TCP_PORT": "40123",
            "DATA_DIR": "/tmp/collector-data",
            "READ_BUFFER_BYTES": "8192",
            "MAX_JSON_LINE_BYTES": "8192",
        }

        config = Config.from_environ(environment)

        self.assertEqual("127.0.0.1", config.host)
        self.assertEqual(40123, config.port)
        self.assertEqual(Path("/tmp/collector-data"), config.data_dir)
        self.assertEqual(8192, config.read_buffer_bytes)
        self.assertEqual(8192, config.max_json_line_bytes)

    def test_rejects_invalid_tcp_port_values(self):
        for value in ("0", "65536", "not-a-number"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "TCP_PORT"):
                    Config.from_environ({"TCP_PORT": value})

    def test_rejects_non_positive_read_buffer_bytes(self):
        with self.assertRaisesRegex(ValueError, "READ_BUFFER_BYTES"):
            Config.from_environ({"READ_BUFFER_BYTES": "0"})
