import contextlib
import io
import os
import runpy
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from revocli.config import load_config
from revocli.tools import filesystem


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class PackageTests(unittest.TestCase):
    def test_script_and_module_entrypoints_show_help(self):
        with tempfile.TemporaryDirectory() as other_directory:
            cases = [
                ([str(PROJECT_ROOT / "main.py"), "--help"], other_directory),
                (["-m", "revocli", "--help"], PROJECT_ROOT),
            ]
            for arguments, directory in cases:
                with self.subTest(arguments=arguments):
                    result = subprocess.run(
                        [sys.executable, "-X", "utf8", *arguments], cwd=directory,
                        capture_output=True, encoding="utf-8", timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("--session", result.stdout)
                    self.assertIn("--workspace", result.stdout)

    def test_module_preview_finds_version_without_loading_model_config(self):
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as file:
            version = tomllib.load(file)["project"]["version"]
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.chdir(directory), \
                patch("revocli.cli.load_config") as config, patch("revocli.cli.OpenAI") as client, \
                patch.object(sys, "argv", ["revocli", "--preview", "--banner", "plain", "--color", "never"]), \
                contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as error:
            runpy.run_module("revocli", run_name="__main__")
        self.assertEqual(error.exception.code, 0)
        self.assertIn(f"RevoCLI  v{version}", output.getvalue())
        config.assert_not_called()
        client.assert_not_called()

    def test_config_and_tool_guard_use_project_root_env_file(self):
        values = {"OPENAI_API_KEY": "offline-key", "OPENAI_MODEL": "offline-model"}
        with patch.dict(os.environ, {}, clear=True), \
                patch("revocli.config.dotenv_values", return_value=values) as dotenv:
            config = load_config()
        dotenv.assert_called_once_with(PROJECT_ROOT / ".env")
        self.assertEqual(config.model, "offline-model")
        self.assertEqual(filesystem.ENV_FILE, PROJECT_ROOT / ".env")


if __name__ == "__main__":
    unittest.main()
