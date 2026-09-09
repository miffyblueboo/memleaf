from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest


class WindowsPublicMcpLauncherTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows GUI launcher acceptance")
    def test_gui_launcher_is_windows_gui_subsystem(self) -> None:
        launcher = shutil.which("memleaf-mcpw")
        self.assertIsNotNone(launcher)
        path = Path(str(launcher))
        with path.open("rb") as stream:
            header = stream.read(64)
            self.assertGreaterEqual(len(header), 64)
            self.assertEqual(header[:2], b"MZ")
            pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
            stream.seek(pe_offset)
            self.assertEqual(stream.read(4), b"PE\x00\x00")
            stream.seek(20, 1)
            optional_start = stream.tell()
            stream.seek(optional_start + 68)
            subsystem = struct.unpack("<H", stream.read(2))[0]
        self.assertEqual(2, subsystem, "memleaf-mcpw.exe must use IMAGE_SUBSYSTEM_WINDOWS_GUI")

    @unittest.skipUnless(os.name == "nt", "Windows GUI launcher acceptance")
    def test_gui_launcher_stdio_mcp_round_trip_without_creation_flags(self) -> None:
        launcher = shutil.which("memleaf-mcpw")
        self.assertIsNotNone(launcher)
        with tempfile.TemporaryDirectory(prefix="memleaf-mcpw-vault-") as temporary:
            vault = Path(temporary) / "vault"
            requests = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "memleaf-windows-test", "version": "1"},
                    },
                },
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            ]
            payload = "".join(json.dumps(item) + "\n" for item in requests)
            process = subprocess.Popen(
                [str(launcher), "--vault", str(vault)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="strict",
                creationflags=0,
            )
            stdout, stderr = process.communicate(payload, timeout=20)
        self.assertEqual(0, process.returncode, stderr)
        responses = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        by_id = {item.get("id"): item for item in responses}
        self.assertIn(1, by_id)
        self.assertIn(2, by_id)
        self.assertIn("result", by_id[1])
        tools = by_id[2]["result"]["tools"]
        self.assertEqual(13, len(tools))
        self.assertEqual(13, len({tool["name"] for tool in tools}))


if __name__ == "__main__":
    unittest.main()
