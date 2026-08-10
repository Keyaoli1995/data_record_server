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

    def test_uses_environment_overrides(self):
        environment = {
            "TCP_HOST": "127.0.0.1",
            "TCP_PORT": "40123",
            "DATA_DIR": "/tmp/collector-data",
            "READ_BUFFER_BYTES": "8192",
        }

        config = Config.from_environ(environment)

        self.assertEqual("127.0.0.1", config.host)
        self.assertEqual(40123, config.port)
        self.assertEqual(Path("/tmp/collector-data"), config.data_dir)
        self.assertEqual(8192, config.read_buffer_bytes)

    def test_rejects_invalid_tcp_port_values(self):
        for value in ("0", "65536", "not-a-number"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "TCP_PORT"):
                    Config.from_environ({"TCP_PORT": value})

    def test_rejects_non_positive_read_buffer_bytes(self):
        with self.assertRaisesRegex(ValueError, "READ_BUFFER_BYTES"):
            Config.from_environ({"READ_BUFFER_BYTES": "0"})
