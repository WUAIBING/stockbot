#!/usr/bin/env python3
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

_TEST_OUTPUT = tempfile.TemporaryDirectory()
os.environ["MX_MONI_OUTPUT_DIR"] = _TEST_OUTPUT.name

_MODULE_SPEC = importlib.util.spec_from_file_location(
    "mx_moni_under_test",
    Path(__file__).with_name("mx_moni.py"),
)
if _MODULE_SPEC is None or _MODULE_SPEC.loader is None:
    raise RuntimeError("Unable to load mx_moni.py for testing")
mx_moni: Any = importlib.util.module_from_spec(_MODULE_SPEC)
_MODULE_SPEC.loader.exec_module(mx_moni)


class ApiRequestRedactionTests(unittest.TestCase):
    original_api_key: str = ""

    def setUp(self):
        self.original_api_key = mx_moni.MX_APIKEY
        mx_moni.MX_APIKEY = "test-super-secret"

    def tearDown(self):
        mx_moni.MX_APIKEY = self.original_api_key

    def capture_request(
        self,
        side_effect: BaseException | None = None,
        result: subprocess.CompletedProcess[str] | None = None,
    ) -> tuple[object | None, str]:
        output = io.StringIO()
        with (
            patch.object(mx_moni.subprocess, "run", side_effect=side_effect, return_value=result),
            redirect_stdout(output),
        ):
            response = mx_moni.api_request("/balance", {})
        return response, output.getvalue()

    def test_timeout_does_not_print_command_or_api_key(self):
        error = subprocess.TimeoutExpired(
            cmd=["curl", "-H", "apikey: test-super-secret"],
            timeout=30,
        )

        response, output = self.capture_request(side_effect=error)

        self.assertIsNone(response)
        self.assertIn("timed out after 30 seconds", output)
        self.assertNotIn("test-super-secret", output)
        self.assertNotIn("apikey:", output)

    def test_generic_exception_only_prints_exception_type(self):
        response, output = self.capture_request(
            side_effect=RuntimeError("apikey: test-super-secret")
        )

        self.assertIsNone(response)
        self.assertIn("RuntimeError", output)
        self.assertNotIn("test-super-secret", output)

    def test_curl_stderr_is_redacted(self):
        result = subprocess.CompletedProcess(
            args=[],
            returncode=2,
            stdout="",
            stderr="request failed for apikey: test-super-secret",
        )

        response, output = self.capture_request(result=result)

        self.assertIsNone(response)
        self.assertIn("<redacted>", output)
        self.assertNotIn("test-super-secret", output)

    def test_invalid_json_preview_is_redacted(self):
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="invalid response containing test-super-secret",
            stderr="",
        )

        response, output = self.capture_request(result=result)

        self.assertIsNone(response)
        self.assertIn("<redacted>", output)
        self.assertNotIn("test-super-secret", output)


if __name__ == "__main__":
    _ = unittest.main()
