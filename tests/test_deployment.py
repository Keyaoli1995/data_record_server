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

from data_record_server.storage import Storage


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
            {
                ".git",
                ".env",
                "__pycache__",
                "*.py[cod]",
                "data",
                "docs",
                "tests",
            }.issubset(
                ignored_paths
            )
        )

    def test_local_docker_environment_file_is_ignored(self):
        git_ignored_paths = {
            line.strip()
            for line in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        self.assertIn(".env", git_ignored_paths)
        result = subprocess.run(
            ["git", "check-ignore", "-q", ".env"],
            cwd=PROJECT_ROOT,
            check=False,
            timeout=5,
        )
        self.assertEqual(0, result.returncode)

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

    def _collector_identity(self):
        if os.getuid() == 0:
            return 10001, 10001
        return os.getuid(), os.getgid()

    def test_prepare_data_dir_script_is_anchored_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository, script = self._copy_preparation_script(temporary_directory)
            data_dir = repository / "data"
            sample_file = data_dir / "connections" / "sample.bin"
            sample_file.parent.mkdir(parents=True)
            sample_file.write_bytes(b"captured bytes")
            external_data_dir = Path(temporary_directory) / "external-data"
            collector_uid, collector_gid = self._collector_identity()
            environment = os.environ | {
                "DATA_DIR": str(external_data_dir),
                "COLLECTOR_UID": str(collector_uid),
                "COLLECTOR_GID": str(collector_gid),
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
            self.assertEqual(collector_uid, data_stat.st_uid)
            self.assertEqual(collector_gid, data_stat.st_gid)
            self.assertEqual(0o750, stat.S_IMODE(data_stat.st_mode))
            self.assertEqual(b"captured bytes", sample_file.read_bytes())
            self.assertTrue(os.access(sample_file, os.R_OK | os.W_OK))

    def test_prepare_data_dir_repairs_key_paths_for_storage(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository, script = self._copy_preparation_script(temporary_directory)
            data_dir = repository / "data"
            connections_dir = data_dir / "connections"
            connections_dir.mkdir(parents=True)
            events_file = data_dir / "events.jsonl"
            sentinel_events = b'{"event":"existing"}\n'
            events_file.write_bytes(sentinel_events)
            connections_dir.chmod(0o500)
            events_file.chmod(0o400)
            collector_uid, collector_gid = self._collector_identity()
            result = subprocess.run(
                [str(script)],
                cwd=repository,
                env=os.environ
                | {
                    "COLLECTOR_UID": str(collector_uid),
                    "COLLECTOR_GID": str(collector_gid),
                },
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(0o750, stat.S_IMODE(data_dir.stat().st_mode))
            self.assertEqual(0o750, stat.S_IMODE(connections_dir.stat().st_mode))
            self.assertEqual(0o640, stat.S_IMODE(events_file.stat().st_mode))
            for path in (data_dir, connections_dir, events_file):
                path_stat = path.stat()
                self.assertEqual(collector_uid, path_stat.st_uid)
                self.assertEqual(collector_gid, path_stat.st_gid)
            self.assertEqual(sentinel_events, events_file.read_bytes())

            storage = Storage(data_dir)
            recorder = storage.open_connection(("127.0.0.1", 30050))
            recorder.record_received(b"probe bytes")
            recorder.close()

            connection_files = list(connections_dir.glob("*.bin"))
            self.assertEqual(1, len(connection_files))
            self.assertEqual(b"probe bytes", connection_files[0].read_bytes())
            updated_events = events_file.read_bytes()
            self.assertTrue(updated_events.startswith(sentinel_events))
            self.assertGreater(len(updated_events), len(sentinel_events))

    def test_prepare_data_dir_script_refuses_a_data_symlink(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository, script = self._copy_preparation_script(temporary_directory)
            sentinel_directory = Path(temporary_directory) / "sentinel"
            sentinel_directory.mkdir()
            sentinel_file = sentinel_directory / "do-not-touch.bin"
            sentinel_file.write_bytes(b"sentinel")
            sentinel_stat = sentinel_directory.stat()
            (repository / "data").symlink_to(sentinel_directory, target_is_directory=True)
            collector_uid, collector_gid = self._collector_identity()

            result = subprocess.run(
                [str(script)],
                cwd=repository,
                env=os.environ
                | {
                    "COLLECTOR_UID": str(collector_uid),
                    "COLLECTOR_GID": str(collector_gid),
                },
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("symlink", result.stderr.lower())
            self.assertEqual(b"sentinel", sentinel_file.read_bytes())
            updated_sentinel_stat = sentinel_directory.stat()
            self.assertEqual(sentinel_stat.st_uid, updated_sentinel_stat.st_uid)
            self.assertEqual(sentinel_stat.st_gid, updated_sentinel_stat.st_gid)
            self.assertEqual(
                stat.S_IMODE(sentinel_stat.st_mode),
                stat.S_IMODE(updated_sentinel_stat.st_mode),
            )

    def test_prepare_data_dir_script_rejects_unsafe_key_paths(self):
        for path_kind in ("connections-symlink", "events-symlink", "events-directory"):
            with self.subTest(path_kind=path_kind), tempfile.TemporaryDirectory() as temporary_directory:
                repository, script = self._copy_preparation_script(temporary_directory)
                data_dir = repository / "data"
                data_dir.mkdir()
                connections_dir = data_dir / "connections"
                events_file = data_dir / "events.jsonl"
                external_path = Path(temporary_directory) / "external"
                if path_kind == "connections-symlink":
                    external_path.mkdir()
                    sentinel_file = external_path / "sentinel.bin"
                    sentinel_file.write_bytes(b"outside")
                    connections_dir.symlink_to(external_path, target_is_directory=True)
                elif path_kind == "events-symlink":
                    connections_dir.mkdir()
                    external_path.write_bytes(b"outside")
                    sentinel_file = external_path
                    events_file.symlink_to(external_path)
                else:
                    connections_dir.mkdir()
                    events_file.mkdir()
                    sentinel_file = events_file / "sentinel.bin"
                    sentinel_file.write_bytes(b"inside unexpected object")
                sentinel_stat = sentinel_file.stat()
                sentinel_bytes = sentinel_file.read_bytes()
                collector_uid, collector_gid = self._collector_identity()

                result = subprocess.run(
                    [str(script)],
                    cwd=repository,
                    env=os.environ
                    | {
                        "COLLECTOR_UID": str(collector_uid),
                        "COLLECTOR_GID": str(collector_gid),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                )

                self.assertNotEqual(0, result.returncode)
                self.assertEqual(sentinel_bytes, sentinel_file.read_bytes())
                updated_sentinel_stat = sentinel_file.stat()
                self.assertEqual(sentinel_stat.st_uid, updated_sentinel_stat.st_uid)
                self.assertEqual(sentinel_stat.st_gid, updated_sentinel_stat.st_gid)
                self.assertEqual(
                    stat.S_IMODE(sentinel_stat.st_mode),
                    stat.S_IMODE(updated_sentinel_stat.st_mode),
                )

    def test_prepare_data_dir_script_rejects_root_collector_identity(self):
        for environment_override in (
            {"COLLECTOR_UID": "0", "COLLECTOR_GID": "10001"},
            {"COLLECTOR_UID": "10001", "COLLECTOR_GID": "0"},
        ):
            with self.subTest(environment_override=environment_override), tempfile.TemporaryDirectory() as temporary_directory:
                repository, script = self._copy_preparation_script(temporary_directory)
                data_dir = repository / "data"
                connections_dir = data_dir / "connections"
                connections_dir.mkdir(parents=True)
                events_file = data_dir / "events.jsonl"
                events_file.write_bytes(b'{"event":"sentinel"}\n')
                original_stats = {
                    path: path.stat() for path in (data_dir, connections_dir, events_file)
                }

                result = subprocess.run(
                    [str(script)],
                    cwd=repository,
                    env=os.environ | environment_override,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                )

                self.assertNotEqual(0, result.returncode)
                self.assertIn("must identify a non-root", result.stderr)
                self.assertEqual(b'{"event":"sentinel"}\n', events_file.read_bytes())
                for path, original_stat in original_stats.items():
                    updated_stat = path.stat()
                    self.assertEqual(original_stat.st_uid, updated_stat.st_uid)
                    self.assertEqual(original_stat.st_gid, updated_stat.st_gid)
                    self.assertEqual(
                        stat.S_IMODE(original_stat.st_mode),
                        stat.S_IMODE(updated_stat.st_mode),
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
            connections_dir.chmod(0o500)
            sample_file.chmod(0o440)
            events_file = data_dir / "events.jsonl"
            events_file.write_bytes(b'{"event":"existing"}\n')
            events_file.chmod(0o400)
            original_stats = {
                path: path.stat()
                for path in (data_dir, connections_dir, sample_file, events_file)
            }
            target_uid = 10001 if os.getuid() == 0 else os.getuid() + 1
            target_gid = 10001 if os.getuid() == 0 else os.getgid() + 1
            environment = os.environ | {
                "COLLECTOR_UID": str(target_uid),
                "COLLECTOR_GID": str(target_gid),
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
                    for path in (data_dir, connections_dir, sample_file, events_file):
                        path_stat = path.stat()
                        self.assertEqual(target_uid, path_stat.st_uid)
                        self.assertEqual(target_gid, path_stat.st_gid)
                    self.assertEqual(0o750, stat.S_IMODE(data_dir.stat().st_mode))
                    self.assertEqual(
                        0o750, stat.S_IMODE(connections_dir.stat().st_mode)
                    )
                    self.assertEqual(0o640, stat.S_IMODE(events_file.stat().st_mode))
                    self.assertEqual(0o440, stat.S_IMODE(sample_file.stat().st_mode))
                    self.assertEqual(b"captured bytes", sample_file.read_bytes())
                    self.assertEqual(b'{"event":"existing"}\n', events_file.read_bytes())
                finally:
                    for path in (sample_file, events_file, connections_dir, data_dir):
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
