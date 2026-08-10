"""Deployment contract tests for the TCP raw-data collector."""

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DeploymentFilesTest(unittest.TestCase):
    def test_dockerfile_declares_the_collector_runtime_contract(self):
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("FROM python:3.12-slim", dockerfile)
        self.assertIn('CMD ["python", "-m", "data_record_server"]', dockerfile)
        self.assertIn("EXPOSE 30050", dockerfile)

    def test_compose_file_declares_the_collector_runtime_contract(self):
        compose_file = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")

        self.assertIn("${TCP_PORT:-30050}:${TCP_PORT:-30050}/tcp", compose_file)
        self.assertIn("./data:/data", compose_file)
        self.assertIn("restart: unless-stopped", compose_file)
        self.assertIn("no-new-privileges:true", compose_file)
        self.assertIn("cap_drop:", compose_file)
