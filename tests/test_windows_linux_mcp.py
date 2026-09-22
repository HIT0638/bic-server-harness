"""Native Windows MCP/Broker/OpenSSH to a disposable Ubuntu WSL1 SSH server."""

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENABLED = os.name == "nt" and bool(os.environ.get("SSHBRIDGE_WSL_DISTRO"))


@unittest.skipUnless(ENABLED, "requires explicitly provisioned Windows/WSL CI target")
class TestWindowsLinuxMcp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.windows_linux_sshd import WslSshd
        cls.target = WslSshd()
        try:
            cls.target.start()
        except BaseException:
            cls.target.close()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.target.close()

    def parameters(self):
        from mcp import StdioServerParameters
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "sshbridge.mcp_server", "--config", str(self.target.config)],
            cwd=str(ROOT), env=self.target.environment)

    async def call(self, client, name, **arguments):
        response = await client.call_tool(name, arguments)
        self.assertFalse(response.is_error, str(response.content))
        result = response.structured_content["result"]
        self.assertNotIn("real_path", result)
        self.assertNotIn("real_cwd", result)
        return result

    async def error(self, client, name, expected, **arguments):
        response = await client.call_tool(name, arguments)
        self.assertTrue(response.is_error)
        text = " ".join(getattr(item, "text", "") for item in response.content)
        self.assertNotIn(self.target.workspace, text)
        self.assertNotIn(str(self.target.base), text)
        data = json.loads(text[text.index("{"):])
        self.assertIn(data["error"]["code"], (expected,) if isinstance(expected, str) else expected)
        return data

    async def until(self, client, job_id, predicate, cursor=0):
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            result = await self.call(client, "exec_status", job_id=job_id, cursor=cursor)
            if predicate(result):
                return result
            await asyncio.sleep(0.05)
        self.fail("Exec job did not reach the required state: %r" % result)

    def test_files_conflicts_binary_paths_and_shared_broker(self):
        from mcp import Client

        async def scenario():
            async with Client(self.parameters(), raise_exceptions=False) as first:
                await self.call(first, "mkdir", path="/mcp 空间")
                path = "/mcp 空间/中文.txt"
                content = "Windows 写入 Ubuntu\n"
                await self.call(first, "write_file", path=path, content=content)
                metadata = await self.call(first, "stat", path=path)
                initial = self.target.client.ping()
                async with Client(self.parameters(), raise_exceptions=False) as second:
                    text = await self.call(second, "read_file", path=path)
                    self.assertEqual(text["content"], content)
                    self.assertEqual(self.target.client.ping()["instance_id"], initial["instance_id"])
                    self.assertEqual(self.target.client.ping()["tcp_generation"], initial["tcp_generation"])
                    digest = await self.call(second, "hash_file", path=path)
                    self.assertEqual(digest["hash"], hashlib.sha256(content.encode()).hexdigest())
                    await self.error(second, "write_file", "CONFLICT", path=path,
                                     content="wrong", expected_size=metadata["size"] + 1)
                    await self.error(second, "write_file", "CONFLICT", path=path,
                                     content="wrong", expected_hash="0" * 64)
                    await self.call(second, "write_file", path=path, content="updated",
                                    expected_mtime=metadata["mtime"], expected_size=metadata["size"],
                                    expected_hash=digest["hash"])
                self.assertEqual(self.target.client.ping()["instance_id"], initial["instance_id"])
                self.assertEqual((await self.call(first, "read_file", path=path))["content"], "updated")
                listing = await self.call(first, "list_dir", path="/mcp 空间")
                self.assertEqual([item["name"] for item in listing["entries"]], ["中文.txt"])
                await self.call(first, "move", src=path, dst="/mcp 空间/moved.txt")
                await self.error(first, "delete", "NOT_EMPTY", path="/mcp 空间")
                await self.error(first, "read_file", "BINARY_FILE", path="/binary.bin")
                binary = await self.call(first, "read_file", path="/binary.bin", encoding="base64")
                self.assertEqual(base64.b64decode(binary["content_b64"]), b"\x00\xffbinary")
                page = await self.call(first, "read_file", path="/large.bin", offset=100, limit=64, encoding="base64")
                self.assertEqual(len(base64.b64decode(page["content_b64"])), 64)
                self.assertTrue(page["truncated"])
                await self.error(first, "read_file", "SANDBOX_VIOLATION", path="/escape/private.txt")
                await self.error(first, "write_file", "SANDBOX_VIOLATION", path="/escape/new.txt", content="bad")
                clamped = await self.call(first, "stat", path="/../../binary.bin")
                self.assertEqual(clamped["size"], 8)
                await self.call(first, "delete", path="/mcp 空间/moved.txt")
                await self.call(first, "delete", path="/mcp 空间")
            self.assertEqual(self.target.client.ping()["instance_id"], initial["instance_id"])

        asyncio.run(scenario())

    def test_sync_async_output_queue_cancel_and_timeout(self):
        from mcp import Client

        async def scenario():
            async with Client(self.parameters(), raise_exceptions=False) as client:
                result = await self.call(client, "exec", command="printf out; printf err >&2; exit 7", timeout=10)
                self.assertEqual((result["stdout"], result["stderr"], result["exit_code"]), ("out", "err", 7))
                await self.call(client, "mkdir", path="/jobs")
                job = await self.call(client, "exec_start", cwd="/jobs", timeout=30,
                                      command="printf first; while [ ! -f release ]; do sleep 0.05; done; printf second")
                first = await self.until(client, job["job_id"], lambda x: any(e["text"] == "first" for e in x["events"]))
                self.assertEqual(first["state"], "RUNNING")
                queued = await self.call(client, "exec_start", command="printf should-not-run", timeout=10)
                canceled = await self.call(client, "exec_cancel", job_id=queued["job_id"])
                self.assertEqual(canceled["state"], "CANCELED")
                self.assertFalse(canceled["remote_termination_unknown"])
                # SFTP must stay responsive while the sole Exec slot is occupied.
                await self.call(client, "write_file", path="/jobs/release", content="go")
                finished = await self.until(client, job["job_id"], lambda x: x["state"] == "EXITED", cursor=first["next_cursor"])
                self.assertEqual("".join(e["text"] for e in finished["events"]), "second")
                running = await self.call(client, "exec_start", command="printf ready; sleep 30", timeout=40)
                await self.until(client, running["job_id"], lambda x: any("ready" in e["text"] for e in x["events"]))
                canceled = await self.call(client, "exec_cancel", job_id=running["job_id"])
                self.assertEqual(canceled["state"], "CANCELED")
                self.assertTrue(canceled["remote_termination_unknown"])
                timed = await self.call(client, "exec_start", command="sleep 30", timeout=1)
                timeout = await self.until(client, timed["job_id"], lambda x: x["state"] == "TIMED_OUT")
                self.assertTrue(timeout["remote_termination_unknown"])
                await self.call(client, "delete", path="/jobs/release")
                await self.call(client, "delete", path="/jobs")

        asyncio.run(scenario())

    def test_disconnect_requires_explicit_reconnect(self):
        from mcp import Client

        async def scenario():
            async with Client(self.parameters(), raise_exceptions=False) as client:
                await self.call(client, "write_file", path="/recovery.txt", content="survives")
                self.target.stop_sshd()
                await self.error(client, "read_file", ("SSH_ERROR", "TIMEOUT"), path="/recovery.txt")
                opened = self.target.client.ping()
                self.assertEqual(opened["state"], "OPEN")
                self.target.start_sshd()
                await self.error(client, "read_file", "CONNECTION_PAUSED", path="/recovery.txt")
                self.assertEqual(self.target.client.ping()["last_attempt_at"], opened["last_attempt_at"])
                restored = await self.call(client, "reconnect")
                self.assertEqual(restored["state"], "READY")
                self.assertEqual((await self.call(client, "read_file", path="/recovery.txt"))["content"], "survives")
                await self.call(client, "delete", path="/recovery.txt")

        asyncio.run(scenario())
