from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from tests.test_hermes_provider import provider_module


class HermesWindowsSubprocessTests(unittest.TestCase):
    def test_windows_creationflags_use_create_no_window(self):
        flag = 0x08000000
        with (
            patch.object(provider_module.os, "name", "nt"),
            patch.object(provider_module.subprocess, "CREATE_NO_WINDOW", flag, create=True),
        ):
            self.assertEqual(provider_module._mcp_creationflags(), flag)

    def test_non_windows_creationflags_are_zero(self):
        with patch.object(provider_module.os, "name", "posix"):
            self.assertEqual(provider_module._mcp_creationflags(), 0)

    def test_mcp_popen_receives_creationflags(self):
        client = provider_module._MCPClient("memleaf-mcp", ".", 5.0)
        process = Mock()
        flag = 0x08000000
        with (
            patch.object(provider_module._MCPClient, "_creationflags", return_value=flag),
            patch.object(provider_module.subprocess, "Popen", return_value=process) as popen,
            patch.object(client, "_resolve_command", return_value="memleaf-mcp"),
            patch.object(client, "_start_stdout_reader_locked"),
            patch.object(client, "_request_locked", return_value={}),
            patch.object(client, "_send_locked"),
        ):
            client._start_locked()

        self.assertEqual(popen.call_args.kwargs.get("creationflags"), flag)
        self.assertIs(popen.call_args.kwargs.get("stdin"), provider_module.subprocess.PIPE)
        self.assertIs(popen.call_args.kwargs.get("stdout"), provider_module.subprocess.PIPE)
        self.assertIs(popen.call_args.kwargs.get("stderr"), provider_module.subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
