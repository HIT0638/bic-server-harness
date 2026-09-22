import base64
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from sshbridge import ops
from sshbridge.broker_client import BrokerClient
from sshbridge.config import Profile
from sshbridge.errors import BridgeError
from sshbridge.sftp_client import SftpSession

try:
    from mcp import Client, StdioServerParameters
    MCP_AVAILABLE = True
except ImportError:
    Client = None
    StdioServerParameters = None
    MCP_AVAILABLE = False

try:
    from tests.local_sshd import LocalSshd, LocalSshdUnavailable
except ImportError:
    from local_sshd import LocalSshd, LocalSshdUnavailable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REMOTE_PY = PROJECT_ROOT / "remote.py"
SENTINEL_NAME = ".sshbridge-integration-sentinel.txt"
SENTINEL_CONTENT = b"sshbridge integration sentinel\n"


class TestLocalSshdIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = LocalSshd()
        try:
            cls.server.start()
            (cls.server.workspace / SENTINEL_NAME).write_bytes(
                SENTINEL_CONTENT)
            cls.profile = Profile("local-test", cls.server.profile_raw)
            cls.session = SftpSession(
                cls.profile.sftp_argv(), cls.profile.op_timeout)
        except LocalSshdUnavailable as exc:
            cls.server.stop()
            raise unittest.SkipTest(str(exc))
        except Exception:
            cls.server.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "session", None) is not None:
            cls.session.shutdown()
        cls.server.stop()

    def setUp(self):
        self.local_case = self.server.workspace / self._testMethodName
        self.local_case.mkdir()
        self.remote_case = "/" + self._testMethodName

    def assert_bridge_error(self, code, function, *args, **kwargs):
        with self.assertRaises(BridgeError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def wait_for_job(self, client, job_id, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        latest = None
        while time.monotonic() < deadline:
            latest = client.request("exec_status", {
                "job_id": job_id,
                "cursor": 0,
                "max_bytes": 65536,
            })
            if predicate(latest):
                return latest
            time.sleep(0.02)
        self.fail("job condition not reached; latest=%r" % latest)

    def test_filesystem_workflow_and_conflicts(self):
        nested = self.remote_case + "/nested"
        made = ops.op_mkdir(
            self.profile, nested, parents=True, session=self.session)
        self.assertEqual(made["op"], "mkdir")

        path = nested + "/data.bin"
        original = b"\x00hello\xff\n"
        written = ops.op_write_file(
            self.profile, path, original, session=self.session)
        self.assertEqual(written["bytes_written"], len(original))
        self.assertFalse(written["overwrote"])

        listing = ops.op_list_dir(
            self.profile, nested, session=self.session)
        self.assertEqual([entry["name"] for entry in listing["entries"]],
                         ["data.bin"])

        stat = ops.op_stat(self.profile, path, session=self.session)
        self.assertEqual(stat["type"], "file")
        self.assertEqual(stat["size"], len(original))

        read = ops.op_read_file(
            self.profile, path, session=self.session)
        self.assertEqual(base64.b64decode(read["content_b64"]), original)
        self.assertFalse(read["truncated"])

        digest = ops.op_hash(self.profile, path, session=self.session)
        self.assertEqual(digest["hash"], hashlib.sha256(original).hexdigest())

        replacement = b"replacement"
        replaced = ops.op_write_file(
            self.profile, path, replacement,
            expected_mtime=stat["mtime"], expected_size=stat["size"],
            expected_hash=digest["hash"], session=self.session)
        self.assertTrue(replaced["overwrote"])

        self.assert_bridge_error(
            "CONFLICT", ops.op_write_file,
            self.profile, path, b"bad", expected_size=999,
            session=self.session)
        self.assert_bridge_error(
            "CONFLICT", ops.op_write_file,
            self.profile, path, b"bad", expected_hash="0" * 64,
            session=self.session)

        moved_path = nested + "/moved.bin"
        moved = ops.op_move(
            self.profile, path, moved_path, session=self.session)
        self.assertFalse(moved["overwrote"])
        final = ops.op_read_file(
            self.profile, moved_path, session=self.session)
        self.assertEqual(base64.b64decode(final["content_b64"]), replacement)

        names = [
            entry["name"] for entry in
            ops.op_list_dir(
                self.profile, nested, session=self.session)["entries"]
        ]
        self.assertEqual(names, ["moved.bin"])
        self.assertFalse(any(name.startswith(".sshbridge.tmp.") for name in names))

    def test_delete_file_and_empty_directory(self):
        directory = self.remote_case + "/delete"
        path = directory + "/note.txt"
        ops.op_mkdir(
            self.profile, directory, session=self.session)
        ops.op_write_file(
            self.profile, path, b"delete me", session=self.session)

        self.assert_bridge_error(
            "NOT_EMPTY", ops.op_delete,
            self.profile, directory, session=self.session)

        deleted_file = ops.op_delete(
            self.profile, path, session=self.session)
        self.assertEqual(deleted_file["op"], "delete")
        self.assertEqual(deleted_file["type"], "file")
        self.assertFalse((self.local_case / "delete" / "note.txt").exists())

        deleted_directory = ops.op_delete(
            self.profile, directory, session=self.session)
        self.assertEqual(deleted_directory["type"], "dir")
        self.assertFalse((self.local_case / "delete").exists())

        self.assert_bridge_error(
            "INVALID_ARG", ops.op_delete,
            self.profile, "/", session=self.session)

    def test_sandbox_blocks_symlink_escape_and_clamps_traversal(self):
        outside_file = self.server.outside / "secret.txt"
        outside_file.write_text("secret", encoding="utf-8")
        os.symlink(self.server.outside, self.local_case / "escape")

        self.assert_bridge_error(
            "SANDBOX_VIOLATION", ops.op_read_file,
            self.profile, self.remote_case + "/escape/secret.txt",
            session=self.session)
        self.assert_bridge_error(
            "SANDBOX_VIOLATION", ops.op_write_file,
            self.profile, self.remote_case + "/escape/new.txt", b"blocked",
            session=self.session)
        self.assert_bridge_error(
            "SANDBOX_VIOLATION", ops.op_delete,
            self.profile, self.remote_case + "/escape",
            session=self.session)
        self.assertTrue(outside_file.exists())

        clamped = ops.op_read_file(
            self.profile, "/../../" + SENTINEL_NAME, session=self.session)
        self.assertEqual(
            base64.b64decode(clamped["content_b64"]),
            SENTINEL_CONTENT)

    def test_read_limits_and_offsets(self):
        content = bytes(range(256)) * 16
        path = self.local_case / "large.bin"
        path.write_bytes(content)
        remote_path = self.remote_case + "/large.bin"

        self.assert_bridge_error(
            "TOO_LARGE", ops.op_read_file,
            self.profile, remote_path, session=self.session)

        chunk = ops.op_read_file(
            self.profile, remote_path, offset=100, limit=4096,
            session=self.session)
        decoded = base64.b64decode(chunk["content_b64"])
        self.assertEqual(decoded, content[100:100 + self.profile.hard_read_cap])
        self.assertEqual(chunk["length"], self.profile.hard_read_cap)
        self.assertTrue(chunk["truncated"])

    def test_exec_structure_cwd_and_timeout(self):
        work = self.local_case / "work"
        work.mkdir()
        cwd = self.remote_case + "/work"
        result = ops.op_exec(
            self.profile,
            "printf stdout; printf stderr >&2; exit 7",
            cwd=cwd, timeout=2)
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["stdout"], "stdout")
        self.assertEqual(result["stderr"], "stderr")
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["real_cwd"], str(work))

        timed_out = ops.op_exec(
            self.profile, "sleep 1", cwd="/", timeout=0.05)
        self.assertIsNone(timed_out["exit_code"])
        self.assertTrue(timed_out["timed_out"])
        self.assertIn("remote process may still be running", timed_out["note"])

    def test_cli_json_round_trip(self):
        read = self.run_cli("--json", "read", "/" + SENTINEL_NAME)
        self.assertEqual(read.returncode, 0, read.stderr)
        payload = json.loads(read.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            base64.b64decode(payload["content_b64"]),
            SENTINEL_CONTENT)

        executed = self.run_cli(
            "--json", "exec", "--cwd", "/", "--", "printf", "cli-ok")
        self.assertEqual(executed.returncode, 0, executed.stderr)
        payload = json.loads(executed.stdout)
        self.assertEqual(payload["stdout"], "cli-ok")
        self.assertEqual(payload["exit_code"], 0)

        local_path = self.local_case / "cli-delete.txt"
        local_path.write_text("delete me", encoding="utf-8")
        deleted = self.run_cli(
            "--json", "rm", self.remote_case + "/cli-delete.txt")
        self.assertEqual(deleted.returncode, 0, deleted.stderr)
        self.assertEqual(json.loads(deleted.stdout)["type"], "file")
        self.assertFalse(local_path.exists())

    def test_daemon_lifecycle_and_routing(self):
        env = os.environ.copy()
        env["SSHBRIDGE_STATE_DIR"] = str(self.server.daemon_state)
        try:
            started = self.run_cli("--json", "daemon", "start", env=env)
            self.assertEqual(started.returncode, 0, started.stderr)
            self.assertTrue(json.loads(started.stdout)["started"])

            listed = self.run_cli("--json", "ls", "/", env=env)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertTrue(json.loads(listed.stdout)["ok"])

            status = self.run_cli("--json", "daemon", "status", env=env)
            self.assertEqual(status.returncode, 0, status.stderr)
            state = json.loads(status.stdout)
            self.assertTrue(state["running"])
            self.assertTrue(state["sftp_alive"])
            self.assertGreaterEqual(state["ops_served"], 1)
        finally:
            self.run_cli("--json", "daemon", "stop", env=env)

    def test_broker_reuses_tcp_and_separates_exec_from_sftp(self):
        previous = os.environ.get("SSHBRIDGE_STATE_DIR")
        os.environ["SSHBRIDGE_STATE_DIR"] = str(self.server.daemon_state)
        client = BrokerClient(str(self.server.config_path), self.profile)
        errors = []
        results = []
        try:
            client.ensure_started()
            for _ in range(100):
                client.request("list_dir", {"path": "/"})
            self.assertEqual(client.status()["tcp_generation"], 1)

            def execute(label):
                try:
                    result = client.request("exec", {
                        "command": "sleep 0.8; printf %s" % label,
                        "cwd": "/",
                        "timeout": 3,
                    })
                    results.append(result["stdout"])
                except Exception as error:
                    errors.append(error)

            threads = [
                threading.Thread(target=execute, args=(label,))
                for label in ("one", "two", "three")
            ]
            for thread in threads:
                thread.start()

            deadline = time.monotonic() + 2
            queued = False
            while time.monotonic() < deadline:
                status = client.status()
                if status["active_exec"] == 2 \
                        and status["queued_exec"] == 1:
                    queued = True
                    break
                time.sleep(0.02)
            self.assertTrue(queued, client.status())

            started = time.monotonic()
            listed = client.request("list_dir", {"path": "/"})
            self.assertEqual(listed["op"], "list_dir")
            self.assertLess(time.monotonic() - started, 0.6)

            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(errors)
            self.assertEqual(sorted(results), ["one", "three", "two"])
            status = client.status()
            self.assertEqual(status["tcp_generation"], 1)
            self.assertEqual(status["active_exec"], 0)
            self.assertEqual(status["queued_exec"], 0)
        finally:
            client.stop()
            if previous is None:
                os.environ.pop("SSHBRIDGE_STATE_DIR", None)
            else:
                os.environ["SSHBRIDGE_STATE_DIR"] = previous

    def test_async_exec_incremental_queue_cancel_timeout_and_cli(self):
        previous = os.environ.get("SSHBRIDGE_STATE_DIR")
        os.environ["SSHBRIDGE_STATE_DIR"] = str(self.server.daemon_state)
        client = BrokerClient(str(self.server.config_path), self.profile)
        try:
            client.ensure_started()
            started_at = time.monotonic()
            started = client.request("exec_start", {
                "command": "printf first; sleep 0.3; printf second",
                "cwd": self.remote_case,
                "timeout": 3,
            })
            self.assertLess(time.monotonic() - started_at, 0.25)
            first = self.wait_for_job(
                client, started["job_id"],
                lambda status: any(
                    event["text"] == "first"
                    for event in status["events"]))
            self.assertEqual(first["state"], "RUNNING")
            final = self.wait_for_job(
                client, started["job_id"],
                lambda status: status["state"] == "EXITED")
            self.assertEqual(final["exit_code"], 0)
            tail = client.request("exec_status", {
                "job_id": started["job_id"],
                "cursor": first["next_cursor"],
                "max_bytes": 65536,
            })
            self.assertEqual(
                "".join(event["text"] for event in tail["events"]),
                "second")

            running = [
                client.request("exec_start", {
                    "command": "sleep 5",
                    "cwd": self.remote_case,
                    "timeout": 10,
                })
                for _ in range(2)
            ]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                snapshot = client.status()
                if snapshot["active_exec"] == 2:
                    break
                time.sleep(0.02)
            self.assertEqual(client.status()["active_exec"], 2)
            with self.assertRaises(BridgeError) as busy:
                client.reconnect()
            self.assertEqual(busy.exception.code, "BROKER_BUSY")

            marker = self.local_case / "queued-marker"
            queued = client.request("exec_start", {
                "command": "printf created > queued-marker",
                "cwd": self.remote_case,
                "timeout": 3,
            })
            self.assertEqual(
                client.request("exec_status", {
                    "job_id": queued["job_id"],
                })["state"],
                "QUEUED")
            queued_cancel = client.request(
                "exec_cancel", {"job_id": queued["job_id"]})
            self.assertEqual(queued_cancel["state"], "CANCELED")
            self.assertFalse(
                queued_cancel["remote_termination_unknown"])

            for job in running:
                canceled = client.request(
                    "exec_cancel", {"job_id": job["job_id"]})
                self.assertEqual(canceled["state"], "CANCELED")
                self.assertTrue(
                    canceled["remote_termination_unknown"])
            time.sleep(0.1)
            self.assertFalse(marker.exists())
            self.assertEqual(
                client.request("list_dir", {"path": "/"})["op"],
                "list_dir")

            timed = client.request("exec_start", {
                "command": "printf before; sleep 5",
                "cwd": self.remote_case,
                "timeout": 0.1,
            })
            timed_status = self.wait_for_job(
                client, timed["job_id"],
                lambda status: status["state"] == "TIMED_OUT")
            self.assertTrue(
                timed_status["remote_termination_unknown"])
            self.assertIn(
                "before",
                "".join(event["text"]
                        for event in timed_status["events"]))

            cli_start = self.run_cli(
                "--json", "exec-start", "--cwd", self.remote_case,
                "--", "printf", "cli-async", env=os.environ.copy())
            self.assertEqual(cli_start.returncode, 0, cli_start.stderr)
            cli_job_id = json.loads(cli_start.stdout)["job_id"]
            deadline = time.monotonic() + 3
            cli_status = None
            while time.monotonic() < deadline:
                completed = self.run_cli(
                    "--json", "exec-status", cli_job_id,
                    env=os.environ.copy())
                self.assertEqual(completed.returncode, 0, completed.stderr)
                cli_status = json.loads(completed.stdout)
                if cli_status["state"] == "EXITED":
                    break
                time.sleep(0.02)
            self.assertEqual(cli_status["state"], "EXITED")
            self.assertEqual(
                "".join(event["text"]
                        for event in cli_status["events"]),
                "cli-async")
            self.assertEqual(client.status()["tcp_generation"], 1)
        finally:
            client.stop()
            if previous is None:
                os.environ.pop("SSHBRIDGE_STATE_DIR", None)
            else:
                os.environ["SSHBRIDGE_STATE_DIR"] = previous

    def test_broker_shutdown_cancels_running_exec_job(self):
        previous = os.environ.get("SSHBRIDGE_STATE_DIR")
        os.environ["SSHBRIDGE_STATE_DIR"] = str(self.server.daemon_state)
        client = BrokerClient(str(self.server.config_path), self.profile)
        stopped = False
        try:
            client.ensure_started()
            started = client.request("exec_start", {
                "command": "sleep 5",
                "cwd": self.remote_case,
                "timeout": 10,
            })
            self.wait_for_job(
                client, started["job_id"],
                lambda status: status["state"] == "RUNNING")
            result = client.stop()
            stopped = True
            self.assertTrue(result["stopped"])
            self.assertEqual(result["canceled_exec_jobs"], 1)
            self.assertTrue(result["remote_termination_unknown"])
        finally:
            if not stopped:
                client.stop()
            if previous is None:
                os.environ.pop("SSHBRIDGE_STATE_DIR", None)
            else:
                os.environ["SSHBRIDGE_STATE_DIR"] = previous

    def test_broker_pauses_after_master_exit_until_reconnect(self):
        previous = os.environ.get("SSHBRIDGE_STATE_DIR")
        os.environ["SSHBRIDGE_STATE_DIR"] = str(self.server.daemon_state)
        client = BrokerClient(str(self.server.config_path), self.profile)
        try:
            client.ensure_started()
            client.request("list_dir", {"path": "/"})
            initial = client.status()
            self.assertEqual(initial["tcp_generation"], 1)

            subprocess.run(
                self.profile.control_argv(
                    client.endpoint.control_path, "exit"),
                capture_output=True, timeout=5)
            deadline = time.monotonic() + 2
            while os.path.exists(client.endpoint.control_path) \
                    and time.monotonic() < deadline:
                time.sleep(0.02)

            with self.assertRaises(BridgeError) as caught:
                client.request("list_dir", {"path": "/"})
            self.assertEqual(caught.exception.code, "CONNECTION_PAUSED")
            self.assertEqual(client.status()["state"], "OPEN")

            time.sleep(0.06)
            reconnected = client.reconnect()
            self.assertEqual(reconnected["state"], "READY")
            self.assertEqual(reconnected["tcp_generation"], 2)
            self.assertEqual(
                client.request("list_dir", {"path": "/"})["op"],
                "list_dir")
        finally:
            client.stop()
            if previous is None:
                os.environ.pop("SSHBRIDGE_STATE_DIR", None)
            else:
                os.environ["SSHBRIDGE_STATE_DIR"] = previous

    def test_broker_first_failure_opens_circuit_without_auto_retry(self):
        bad_config = self.server.base / "bridge.bad.json"
        bad_profile_raw = dict(self.server.profile_raw)
        bad_profile_raw["port"] = 1
        bad_policy = dict(bad_profile_raw["connection_policy"])
        bad_policy["cooldown_initial"] = 60
        bad_policy["cooldown_max"] = 60
        bad_profile_raw["connection_policy"] = bad_policy
        with open(bad_config, "w", encoding="utf-8") as stream:
            json.dump({
                "default_profile": "bad",
                "profiles": {"bad": bad_profile_raw},
            }, stream)
        bad_profile = Profile("bad", bad_profile_raw)
        previous = os.environ.get("SSHBRIDGE_STATE_DIR")
        os.environ["SSHBRIDGE_STATE_DIR"] = str(self.server.daemon_state)
        client = BrokerClient(str(bad_config), bad_profile)
        try:
            client.ensure_started()
            with self.assertRaises(BridgeError) as first:
                client.request("list_dir", {"path": "/"})
            self.assertIn(first.exception.code, ("SSH_ERROR", "TIMEOUT"))
            opened = client.status()
            self.assertEqual(opened["state"], "OPEN")
            self.assertEqual(opened["failure_count"], 1)
            attempted_at = opened["last_attempt_at"]

            with self.assertRaises(BridgeError) as second:
                client.request("list_dir", {"path": "/"})
            self.assertEqual(second.exception.code, "CONNECTION_PAUSED")
            paused = client.status()
            self.assertEqual(paused["failure_count"], 1)
            self.assertEqual(paused["last_attempt_at"], attempted_at)
            self.assertGreater(paused["cooldown_until"], time.time() + 50)

            time.sleep(0.06)
            with self.assertRaises(BridgeError) as retry:
                client.reconnect()
            self.assertIn(retry.exception.code, ("SSH_ERROR", "TIMEOUT"))
            self.assertEqual(client.status()["failure_count"], 2)
        finally:
            client.stop()
            if previous is None:
                os.environ.pop("SSHBRIDGE_STATE_DIR", None)
            else:
                os.environ["SSHBRIDGE_STATE_DIR"] = previous

    def test_broker_without_controlmaster_serializes_exec(self):
        direct_config = self.server.base / "bridge.no-mux.json"
        direct_profile_raw = dict(self.server.profile_raw)
        policy = dict(direct_profile_raw["connection_policy"])
        policy["control_master"] = False
        policy["min_connect_interval"] = 0
        direct_profile_raw["connection_policy"] = policy
        with open(direct_config, "w", encoding="utf-8") as stream:
            json.dump({
                "default_profile": "no-mux",
                "profiles": {"no-mux": direct_profile_raw},
            }, stream)
        direct_profile = Profile("no-mux", direct_profile_raw)
        previous = os.environ.get("SSHBRIDGE_STATE_DIR")
        os.environ["SSHBRIDGE_STATE_DIR"] = str(self.server.daemon_state)
        client = BrokerClient(str(direct_config), direct_profile)
        results = []
        try:
            client.ensure_started()
            client.request("list_dir", {"path": "/"})
            initial = client.status()
            self.assertFalse(initial["multiplexing"])
            self.assertEqual(initial["exec_concurrency"], 1)
            self.assertIn("disabled", initial["degraded_reason"])
            self.assertEqual(initial["tcp_generation"], 1)

            def execute(label):
                results.append(client.request("exec", {
                    "command": "sleep 0.3; printf %s" % label,
                    "cwd": "/",
                    "timeout": 2,
                })["stdout"])

            started = time.monotonic()
            threads = [
                threading.Thread(target=execute, args=(label,))
                for label in ("one", "two")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 0.5)
            self.assertEqual(sorted(results), ["one", "two"])
            status = client.status()
            self.assertEqual(status["tcp_generation"], 3)
            self.assertEqual(status["active_exec"], 0)
        finally:
            client.stop()
            if previous is None:
                os.environ.pop("SSHBRIDGE_STATE_DIR", None)
            else:
                os.environ["SSHBRIDGE_STATE_DIR"] = previous

    @unittest.skipUnless(MCP_AVAILABLE, "MCP Python SDK is not installed")
    def test_mcp_stdio_workflow_and_shared_broker(self):
        previous = os.environ.get("SSHBRIDGE_STATE_DIR")
        os.environ["SSHBRIDGE_STATE_DIR"] = str(self.server.daemon_state)
        broker = BrokerClient(str(self.server.config_path), self.profile)

        async def workflow():
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-m", "sshbridge.mcp_server",
                    "--config", str(self.server.config_path),
                    "--profile", "local-test",
                ],
                env={
                    "SSHBRIDGE_STATE_DIR": str(self.server.daemon_state),
                },
                cwd=str(PROJECT_ROOT),
            )
            directory = self.remote_case + "/mcp"
            text_path = directory + "/note.txt"
            moved_path = directory + "/moved.txt"
            binary_path = directory + "/binary.bin"

            async with Client(parameters) as first:
                tools = await first.list_tools()
                self.assertEqual(len(tools.tools), 14)

                reconnected = await first.call_tool("reconnect", {})
                self.assertFalse(reconnected.is_error)
                self.assertEqual(
                    reconnected.structured_content["result"][
                        "tcp_generation"],
                    1)

                made = await first.call_tool(
                    "mkdir", {"path": directory})
                self.assertFalse(made.is_error)
                written = await first.call_tool(
                    "write_file",
                    {"path": text_path, "content": "mcp text\n"})
                self.assertEqual(
                    written.structured_content["result"]["bytes_written"],
                    9)

                listed = await first.call_tool(
                    "list_dir", {"path": directory})
                self.assertEqual(
                    [entry["name"] for entry in
                     listed.structured_content["result"]["entries"]],
                    ["note.txt"])

                metadata = await first.call_tool(
                    "stat", {"path": text_path})
                self.assertEqual(
                    metadata.structured_content["result"]["type"], "file")
                self.assertNotIn(
                    "real_path", metadata.structured_content["result"])

                read = await first.call_tool(
                    "read_file", {"path": text_path})
                self.assertEqual(
                    read.structured_content["result"]["content"],
                    "mcp text\n")
                digest = await first.call_tool(
                    "hash_file", {"path": text_path})
                self.assertEqual(
                    digest.structured_content["result"]["hash"],
                    hashlib.sha256(b"mcp text\n").hexdigest())

                moved = await first.call_tool(
                    "move", {"src": text_path, "dst": moved_path})
                self.assertFalse(moved.is_error)

                (self.local_case / "mcp" / "binary.bin").write_bytes(
                    b"\x00\xffmcp")
                binary = await first.call_tool(
                    "read_file",
                    {"path": binary_path, "encoding": "base64"})
                self.assertEqual(
                    base64.b64decode(
                        binary.structured_content["result"]["content_b64"]),
                    b"\x00\xffmcp")

                executed = await first.call_tool(
                    "exec",
                    {
                        "command": "printf mcp-ok",
                        "cwd": directory,
                        "timeout": 2,
                    })
                self.assertEqual(
                    executed.structured_content["result"]["stdout"],
                    "mcp-ok")
                self.assertNotIn(
                    "real_cwd", executed.structured_content["result"])

                async_started = await first.call_tool(
                    "exec_start",
                    {
                        "command": "printf mcp-first; sleep 0.2; "
                                   "printf mcp-second",
                        "cwd": directory,
                        "timeout": 2,
                    })
                self.assertFalse(async_started.is_error)
                async_job_id = (
                    async_started.structured_content["result"]["job_id"])
                first_page = None
                for _ in range(100):
                    first_page = await first.call_tool(
                        "exec_status",
                        {"job_id": async_job_id})
                    if any(
                            event["text"] == "mcp-first"
                            for event in first_page.structured_content[
                                "result"]["events"]):
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(
                    first_page.structured_content["result"]["state"],
                    "RUNNING")
                cursor = first_page.structured_content[
                    "result"]["next_cursor"]

                conflict = await first.call_tool(
                    "write_file",
                    {
                        "path": moved_path,
                        "content": "bad",
                        "expected_size": 999,
                    })
                self.assertEqual(
                    self.mcp_error_code(conflict), "CONFLICT")
                not_empty = await first.call_tool(
                    "delete", {"path": directory})
                self.assertEqual(
                    self.mcp_error_code(not_empty), "NOT_EMPTY")

                status = await first.call_tool(
                    "connection_status", {})
                self.assertEqual(
                    status.structured_content["result"]["tcp_generation"],
                    1)
                async with Client(parameters) as second:
                    second_status = await second.call_tool(
                        "connection_status", {})
                    self.assertEqual(
                        second_status.structured_content["result"][
                            "tcp_generation"],
                        1)
                    async_status = None
                    for _ in range(100):
                        async_status = await second.call_tool(
                            "exec_status",
                            {
                                "job_id": async_job_id,
                                "cursor": cursor,
                            })
                        if async_status.structured_content[
                                "result"]["state"] == "EXITED":
                            break
                        await asyncio.sleep(0.02)
                    self.assertFalse(async_status.is_error)
                    self.assertEqual(
                        "".join(
                            event["text"] for event in
                            async_status.structured_content[
                                "result"]["events"]),
                        "mcp-second")

                for path in (moved_path, binary_path, directory):
                    deleted = await first.call_tool(
                        "delete", {"path": path})
                    self.assertFalse(deleted.is_error)

        try:
            asyncio.run(workflow())
            self.assertTrue(broker.status()["running"])
            self.assertEqual(broker.status()["tcp_generation"], 1)
            quiet = subprocess.run(
                [
                    sys.executable, "-m", "sshbridge.mcp_server",
                    "--config", str(self.server.config_path),
                    "--profile", "local-test",
                ],
                cwd=PROJECT_ROOT, env=os.environ.copy(),
                input="", capture_output=True, text=True, timeout=20)
            self.assertEqual(quiet.returncode, 0, quiet.stderr)
            self.assertEqual(quiet.stdout, "")
            listed = self.run_cli(
                "--json", "ls", "/", env=os.environ.copy())
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(broker.status()["tcp_generation"], 1)
        finally:
            broker.stop()
            if previous is None:
                os.environ.pop("SSHBRIDGE_STATE_DIR", None)
            else:
                os.environ["SSHBRIDGE_STATE_DIR"] = previous

    @unittest.skipUnless(MCP_AVAILABLE, "MCP Python SDK is not installed")
    def test_mcp_reconnect_recovers_paused_broker(self):
        previous = os.environ.get("SSHBRIDGE_STATE_DIR")
        os.environ["SSHBRIDGE_STATE_DIR"] = str(self.server.daemon_state)
        broker = BrokerClient(str(self.server.config_path), self.profile)

        async def workflow():
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-m", "sshbridge.mcp_server",
                    "--config", str(self.server.config_path),
                    "--profile", "local-test",
                ],
                env={
                    "SSHBRIDGE_STATE_DIR": str(self.server.daemon_state),
                },
                cwd=str(PROJECT_ROOT),
            )
            async with Client(parameters) as client:
                listed = await client.call_tool("list_dir", {"path": "/"})
                self.assertFalse(listed.is_error)
                self.assertEqual(broker.status()["tcp_generation"], 1)

                subprocess.run(
                    self.profile.control_argv(
                        broker.endpoint.control_path, "exit"),
                    capture_output=True, timeout=5)
                deadline = time.monotonic() + 2
                while os.path.exists(broker.endpoint.control_path) \
                        and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)

                paused = await client.call_tool(
                    "list_dir", {"path": "/"})
                self.assertEqual(
                    self.mcp_error_code(paused), "CONNECTION_PAUSED")
                await asyncio.sleep(0.06)
                reconnected = await client.call_tool("reconnect", {})
                self.assertFalse(reconnected.is_error)
                self.assertEqual(
                    reconnected.structured_content["result"][
                        "tcp_generation"],
                    2)
                recovered = await client.call_tool(
                    "list_dir", {"path": "/"})
                self.assertFalse(recovered.is_error)

        try:
            asyncio.run(workflow())
        finally:
            broker.stop()
            if previous is None:
                os.environ.pop("SSHBRIDGE_STATE_DIR", None)
            else:
                os.environ["SSHBRIDGE_STATE_DIR"] = previous

    @staticmethod
    def mcp_error_code(result):
        text = "\n".join(
            item.text for item in result.content
            if getattr(item, "type", None) == "text")
        start = text.find("{")
        if start < 0:
            raise AssertionError(
                "MCP error did not contain JSON: %r" % text)
        return json.loads(text[start:])["error"]["code"]

    def run_cli(self, *arguments, env=None):
        return subprocess.run(
            [sys.executable, str(REMOTE_PY),
             "--config", str(self.server.config_path)] + list(arguments),
            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
            timeout=30)


def _compatible_rsync():
    candidates = [
        "/opt/homebrew/bin/rsync",
        "/usr/local/bin/rsync",
        shutil.which("rsync"),
    ]
    for candidate in candidates:
        if not candidate or not os.path.isfile(candidate):
            continue
        completed = subprocess.run(
            [candidate, "--help"],
            capture_output=True, text=True, timeout=5)
        if completed.returncode == 0 \
                and (
                    "--protect-args" in completed.stdout
                    or "--secluded-args" in completed.stdout):
            return os.path.abspath(candidate)
    return None


class TestRsyncLocalSshdIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rsync_bin = _compatible_rsync()
        if cls.rsync_bin is None:
            raise unittest.SkipTest(
                "compatible rsync with --protect-args is unavailable")
        cls.server = LocalSshd()
        cls.previous_state_dir = os.environ.get("SSHBRIDGE_STATE_DIR")
        cls.local_temp = tempfile.TemporaryDirectory(
            prefix="sshbridge-rsync-local-")
        try:
            cls.server.start()
            os.environ["SSHBRIDGE_STATE_DIR"] = str(
                cls.server.daemon_state)
            with open(
                    cls.server.config_path, "r",
                    encoding="utf-8") as stream:
                config = json.load(stream)
            raw = config["profiles"]["local-test"]
            raw["rsync_bin"] = cls.rsync_bin
            raw["remote_rsync_bin"] = cls.rsync_bin
            with open(
                    cls.server.config_path, "w",
                    encoding="utf-8") as stream:
                json.dump(config, stream, indent=2)
                stream.write("\n")
            cls.profile = Profile("local-test", raw)
            cls.client = BrokerClient(
                str(cls.server.config_path), cls.profile)
            cls.client.ensure_started()
        except LocalSshdUnavailable as error:
            cls.server.stop()
            cls.local_temp.cleanup()
            raise unittest.SkipTest(str(error))
        except Exception:
            cls.server.stop()
            cls.local_temp.cleanup()
            cls._restore_state_dir()
            raise

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "client", None) is not None:
            cls.client.stop()
        cls.server.stop()
        cls.local_temp.cleanup()
        cls._restore_state_dir()

    @classmethod
    def _restore_state_dir(cls):
        if cls.previous_state_dir is None:
            os.environ.pop("SSHBRIDGE_STATE_DIR", None)
        else:
            os.environ["SSHBRIDGE_STATE_DIR"] = cls.previous_state_dir

    def wait_job(self, job_id, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.client.request(
                "sync_status", {"job_id": job_id}, timeout=5)
            if result["state"] in ("succeeded", "failed", "cancelled"):
                return result
            time.sleep(0.02)
        self.fail("sync job did not finish: %s" % job_id)

    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, str(REMOTE_PY),
             "--config", str(self.server.config_path)] + list(arguments),
            cwd=PROJECT_ROOT, env=os.environ.copy(),
            capture_output=True, text=True, timeout=30)

    def test_real_push_pull_and_cli_status(self):
        local_root = Path(self.local_temp.name) / "real-flow"
        local_root.mkdir()
        source_file = local_root / "one file.txt"
        source_file.write_text("one\n", encoding="utf-8")
        source_dir = local_root / "source dir"
        source_dir.mkdir()
        (source_dir / "nested.txt").write_text(
            "nested\n", encoding="utf-8")

        remote_destination = self.server.workspace / "sync target"
        remote_destination.mkdir()
        started = self.run_cli(
            "--json", "sync", "push",
            str(source_file), str(source_dir),
            "--to", "/sync target")
        self.assertEqual(started.returncode, 0, started.stderr)
        push = json.loads(started.stdout)
        self.assertTrue(push["ok"])
        pushed = self.wait_job(push["job_id"])
        self.assertEqual(pushed["state"], "succeeded", pushed)
        self.assertGreaterEqual(pushed["files_transferred"], 2)
        self.assertEqual(
            (remote_destination / "one file.txt").read_text(
                encoding="utf-8"),
            "one\n")
        self.assertEqual(
            (remote_destination / "source dir" / "nested.txt").read_text(
                encoding="utf-8"),
            "nested\n")

        status = self.run_cli(
            "--json", "sync", "status", push["job_id"])
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["state"], "succeeded")

        remote_source = self.server.workspace / "pull source"
        remote_source.mkdir()
        (remote_source / "remote.txt").write_text(
            "remote\n", encoding="utf-8")
        local_destination = local_root / "downloads"
        local_destination.mkdir()
        pull = self.client.request("sync_start", {
            "direction": "pull",
            "sources": ["/pull source"],
            "destination": str(local_destination),
        })
        pulled = self.wait_job(pull["job_id"])
        self.assertEqual(pulled["state"], "succeeded", pulled)
        self.assertEqual(
            (local_destination / "pull source" / "remote.txt").read_text(
                encoding="utf-8"),
            "remote\n")
        self.assertEqual(self.client.status()["tcp_generation"], 1)

    def test_symlink_escape_is_rejected_for_push_and_pull(self):
        outside_file = self.server.outside / "secret.txt"
        outside_file.write_text("secret", encoding="utf-8")
        escape = self.server.workspace / "rsync-escape"
        os.symlink(self.server.outside, escape)
        source = Path(self.local_temp.name) / "safe-source.txt"
        source.write_text("safe", encoding="utf-8")

        for direction, sources, destination in (
                ("push", [str(source)], "/rsync-escape"),
                ("pull", ["/rsync-escape/secret.txt"],
                 self.local_temp.name)):
            with self.subTest(direction=direction):
                with self.assertRaises(BridgeError) as caught:
                    self.client.request("sync_start", {
                        "direction": direction,
                        "sources": sources,
                        "destination": destination,
                    })
                self.assertEqual(
                    caught.exception.code, "SANDBOX_VIOLATION")
        self.assertEqual(outside_file.read_text(encoding="utf-8"), "secret")

    def test_incompatible_remote_rsync_does_not_break_sftp(self):
        bad_config = self.server.base / "bridge.rsync-old.json"
        raw = dict(self.server.profile_raw)
        raw["rsync_bin"] = self.rsync_bin
        raw["remote_rsync_bin"] = "/usr/bin/rsync"
        with open(bad_config, "w", encoding="utf-8") as stream:
            json.dump({
                "default_profile": "old-rsync",
                "profiles": {"old-rsync": raw},
            }, stream)
            stream.write("\n")
        profile = Profile("old-rsync", raw)
        client = BrokerClient(str(bad_config), profile)
        source = Path(self.local_temp.name) / "old-rsync-source.txt"
        source.write_text("source", encoding="utf-8")
        try:
            client.ensure_started()
            with self.assertRaises(BridgeError) as caught:
                client.request("sync_start", {
                    "direction": "push",
                    "sources": [str(source)],
                    "destination": "/",
                })
            self.assertEqual(
                caught.exception.code, "RSYNC_UNAVAILABLE")
            self.assertEqual(
                caught.exception.details["stage"], "remote")
            self.assertEqual(
                client.request("list_dir", {"path": "/"})["op"],
                "list_dir")
            self.assertEqual(client.status()["state"], "READY")
        finally:
            client.stop()


if __name__ == "__main__":
    unittest.main()
