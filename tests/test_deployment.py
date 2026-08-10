"""Deployment contract tests for the TCP raw-data collector."""

import json
import unittest
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UID = "10001"
DEFAULT_GID = "10001"
UID_VARIABLE = "${COLLECTOR_UID:-10001}"
GID_VARIABLE = "${COLLECTOR_GID:-10001}"


def _dockerfile_instructions():
    """Returns non-comment Dockerfile instructions with continuations joined."""
    instructions = []
    pending = ""
    for line in (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        instructions.append(pending)
        pending = ""
    if pending:
        raise AssertionError("Dockerfile ends with an incomplete instruction")
    return instructions


class DeploymentFilesTest(unittest.TestCase):
    def test_dockerfile_declares_the_collector_runtime_contract(self):
        instructions = _dockerfile_instructions()
        environment = " ".join(
            instruction for instruction in instructions if instruction.startswith("ENV ")
        )

        self.assertEqual("FROM python:3.12-slim", instructions[0])
        for name, value in {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "TCP_HOST": "0.0.0.0",
            "TCP_PORT": "30050",
            "DATA_DIR": "/data",
            "READ_BUFFER_BYTES": "4096",
        }.items():
            self.assertIn(f"{name}={value}", environment)
        self.assertIn(f"ARG COLLECTOR_UID={DEFAULT_UID}", instructions)
        self.assertIn(f"ARG COLLECTOR_GID={DEFAULT_GID}", instructions)
        self.assertTrue(
            any("groupadd" in instruction and "useradd" in instruction for instruction in instructions),
            "Dockerfile must create the collector group and user",
        )
        self.assertTrue(
            any(
                "/data" in instruction
                and "collector" in instruction
                and ("chown" in instruction or "install -d" in instruction)
                for instruction in instructions
            ),
            "Dockerfile must make /data writable by collector",
        )
        self.assertIn("WORKDIR /app", instructions)
        self.assertIn("COPY data_record_server/ /app/data_record_server/", instructions)
        self.assertIn("EXPOSE 30050", instructions)
        self.assertEqual(
            ["python", "-m", "data_record_server"],
            json.loads(next(instruction[4:] for instruction in instructions if instruction.startswith("CMD "))),
        )
        self.assertEqual("USER collector", instructions[-1])

    def test_compose_file_declares_the_collector_runtime_contract(self):
        compose = yaml.safe_load((PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8"))
        collector = compose["services"]["collector"]

        self.assertEqual(
            {
                "context": ".",
                "args": {"COLLECTOR_UID": UID_VARIABLE, "COLLECTOR_GID": GID_VARIABLE},
            },
            collector["build"],
        )
        self.assertEqual(
            {
                "TCP_HOST": "0.0.0.0",
                "TCP_PORT": "${TCP_PORT:-30050}",
                "DATA_DIR": "/data",
                "READ_BUFFER_BYTES": "${READ_BUFFER_BYTES:-4096}",
            },
            collector["environment"],
        )
        self.assertEqual(
            ["${TCP_PORT:-30050}:${TCP_PORT:-30050}/tcp"], collector["ports"]
        )
        self.assertEqual(["./data:/data"], collector["volumes"])
        self.assertEqual("unless-stopped", collector["restart"])
        self.assertTrue(collector["init"])
        self.assertTrue(collector["read_only"])
        self.assertIn("/tmp", collector["tmpfs"])
        self.assertEqual(["no-new-privileges:true"], collector["security_opt"])
        self.assertEqual(["ALL"], collector["cap_drop"])
        self.assertEqual(f"{UID_VARIABLE}:{GID_VARIABLE}", collector["user"])

    def test_dockerignore_excludes_non_runtime_project_files(self):
        ignored_paths = {
            line.strip()
            for line in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        self.assertTrue(
            {".git", "__pycache__", "*.py[cod]", "data", "docs", "tests"}.issubset(
                ignored_paths
            )
        )
