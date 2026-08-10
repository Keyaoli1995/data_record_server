"""Deployment contract tests for the TCP raw-data collector."""

import json
import os
import shutil
import stat
import subprocess
import tempfile
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

    def _copy_preparation_script(self, temporary_directory):
        repository = Path(temporary_directory) / "repo"
        scripts_directory = repository / "scripts"
        scripts_directory.mkdir(parents=True)
        script = PROJECT_ROOT / "scripts" / "prepare-data-dir.sh"
        self.assertTrue(script.is_file(), "bind-mount preparation script must exist")
        self.assertTrue(
            script.stat().st_mode & stat.S_IXUSR,
            "bind-mount preparation script must be executable",
        )
        copied_script = scripts_directory / script.name
        shutil.copy2(script, copied_script)
        return repository, copied_script

    def test_prepare_data_dir_script_is_anchored_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository, script = self._copy_preparation_script(temporary_directory)
            data_dir = repository / "data"
            sample_file = data_dir / "connections" / "sample.bin"
            sample_file.parent.mkdir(parents=True)
            sample_file.write_bytes(b"captured bytes")
            external_data_dir = Path(temporary_directory) / "external-data"
            environment = os.environ | {
                "DATA_DIR": str(external_data_dir),
                "COLLECTOR_UID": str(os.getuid()),
                "COLLECTOR_GID": str(os.getgid()),
            }
            first_result = subprocess.run(
                [str(script)],
                cwd=repository,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            second_result = subprocess.run(
                [str(script)],
                cwd=repository,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            self.assertEqual(0, first_result.returncode, first_result.stderr)
            self.assertEqual(0, second_result.returncode, second_result.stderr)
            self.assertFalse(external_data_dir.exists())
            data_stat = data_dir.stat()
            self.assertTrue(data_dir.is_dir())
            self.assertEqual(os.getuid(), data_stat.st_uid)
            self.assertEqual(os.getgid(), data_stat.st_gid)
            self.assertEqual(0o750, stat.S_IMODE(data_stat.st_mode))
            self.assertEqual(b"captured bytes", sample_file.read_bytes())
            self.assertTrue(os.access(sample_file, os.R_OK | os.W_OK))

    def test_prepare_data_dir_script_refuses_a_data_symlink(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository, script = self._copy_preparation_script(temporary_directory)
            sentinel_directory = Path(temporary_directory) / "sentinel"
            sentinel_directory.mkdir()
            sentinel_file = sentinel_directory / "do-not-touch.bin"
            sentinel_file.write_bytes(b"sentinel")
            sentinel_stat = sentinel_directory.stat()
            (repository / "data").symlink_to(sentinel_directory, target_is_directory=True)

            result = subprocess.run(
                [str(script)],
                cwd=repository,
                env=os.environ
                | {
                    "COLLECTOR_UID": str(os.getuid()),
                    "COLLECTOR_GID": str(os.getgid()),
                },
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(b"sentinel", sentinel_file.read_bytes())
            updated_sentinel_stat = sentinel_directory.stat()
            self.assertEqual(sentinel_stat.st_uid, updated_sentinel_stat.st_uid)
            self.assertEqual(sentinel_stat.st_gid, updated_sentinel_stat.st_gid)
            self.assertEqual(
                stat.S_IMODE(sentinel_stat.st_mode),
                stat.S_IMODE(updated_sentinel_stat.st_mode),
            )

    def test_rejects_existing_tree_with_mismatched_ownership_without_root(self):
        script_source = (PROJECT_ROOT / "scripts" / "prepare-data-dir.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'chown -R -h -- "$COLLECTOR_UID:$COLLECTOR_GID" "$DATA_DIR"',
            script_source,
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository, script = self._copy_preparation_script(temporary_directory)
            data_dir = repository / "data"
            data_dir.mkdir()
            connections_dir = data_dir / "connections"
            connections_dir.mkdir()
            sample_file = connections_dir / "sample.bin"
            sample_file.write_bytes(b"captured bytes")
            original_stats = {
                path: path.stat() for path in (data_dir, connections_dir, sample_file)
            }
            environment = os.environ | {
                "COLLECTOR_UID": "10001",
                "COLLECTOR_GID": "10001",
            }
            first_result = subprocess.run(
                [str(script)],
                cwd=repository,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )

            if os.getuid() == 0:
                try:
                    second_result = subprocess.run(
                        [str(script)],
                        cwd=repository,
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=5,
                    )
                    self.assertEqual(0, first_result.returncode, first_result.stderr)
                    self.assertEqual(0, second_result.returncode, second_result.stderr)
                    for path in (data_dir, connections_dir, sample_file):
                        path_stat = path.stat()
                        self.assertEqual(10001, path_stat.st_uid)
                        self.assertEqual(10001, path_stat.st_gid)
                    self.assertEqual(0o750, stat.S_IMODE(data_dir.stat().st_mode))
                    self.assertEqual(b"captured bytes", sample_file.read_bytes())
                finally:
                    for path in (sample_file, connections_dir, data_dir):
                        os.chown(path, os.getuid(), os.getgid())
            else:
                self.assertNotEqual(0, first_result.returncode)
                self.assertIn("sudo", first_result.stderr)
                self.assertEqual(b"captured bytes", sample_file.read_bytes())
                for path, original_stat in original_stats.items():
                    updated_stat = path.stat()
                    self.assertEqual(original_stat.st_uid, updated_stat.st_uid)
                    self.assertEqual(original_stat.st_gid, updated_stat.st_gid)
                    self.assertEqual(
                        stat.S_IMODE(original_stat.st_mode),
                        stat.S_IMODE(updated_stat.st_mode),
                    )
