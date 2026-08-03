from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RESTART_SCRIPT = REPO_ROOT / "scripts" / "restart.sh"


def write_fake_python(path: Path, version: str, pip_version: str) -> None:
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then\n'
        '  case "$2" in\n'
        f'    *version_info*) printf "%s\\n" "{version}" ;;\n'
        '    *) printf "null\\n" ;;\n'
        "  esac\n"
        'elif [ "$1" = "-m" ] && [ "$2" = "pip" ]; then\n'
        f'  printf "%s\\n" "pip {pip_version} from fake (python {version})"\n'
        "fi\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


class RestartScriptTest(unittest.TestCase):
    def make_script_fixture(self, temp_dir: Path) -> tuple[Path, Path, Path]:
        repo_root = temp_dir / "repo"
        script_dir = repo_root / "scripts"
        desktop_dir = repo_root / "zero_agent" / "frontends" / "desktop"
        script_dir.mkdir(parents=True)
        desktop_dir.mkdir(parents=True)
        shutil.copy2(RESTART_SCRIPT, script_dir / "restart.sh")
        return repo_root, desktop_dir, script_dir / "restart.sh"

    def test_restart_falls_back_from_incompatible_virtualenv_and_old_python3(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_dir = Path(temporary_directory)
            repo_root, _, restart_script = self.make_script_fixture(temp_dir)
            venv_python = repo_root / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            write_fake_python(venv_python, "3.9", "21.2.4")

            bin_dir = temp_dir / "bin"
            bin_dir.mkdir()
            write_fake_python(bin_dir / "python3", "3.9", "21.2.4")
            write_fake_python(bin_dir / "python3.11", "3.11", "26.0.1")

            environment = os.environ | {
                "BRIDGE_PORT": "54168",
                "HOME": temporary_directory,
                "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
                "PYTHON": "",
            }
            result = subprocess.run(
                ["/bin/bash", str(restart_script), "--dry-run", "--stop-only"],
                cwd=repo_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"[restart] python: {bin_dir / 'python3.11'}", result.stdout)

    def test_restart_rejects_supported_python_with_unsupported_pip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_dir = Path(temporary_directory)
            repo_root, _, restart_script = self.make_script_fixture(temp_dir)
            python = temp_dir / "python3.11"
            write_fake_python(python, "3.11", "21.2.4")

            result = subprocess.run(
                ["/bin/bash", str(restart_script), "--dry-run", "--stop-only"],
                cwd=repo_root,
                env=os.environ | {"BRIDGE_PORT": "54168", "HOME": temporary_directory, "PYTHON": str(python)},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pip must support PEP 660 editable installs", result.stderr)

    def test_restart_rejects_explicit_incompatible_python(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_dir = Path(temporary_directory)
            repo_root, _, restart_script = self.make_script_fixture(temp_dir)
            bin_dir = temp_dir / "bin"
            bin_dir.mkdir()
            unsupported_python = bin_dir / "python3.9"
            write_fake_python(unsupported_python, "3.9", "26.0.1")
            write_fake_python(bin_dir / "python3.11", "3.11", "26.0.1")

            result = subprocess.run(
                ["/bin/bash", str(restart_script), "--dry-run", "--stop-only"],
                cwd=repo_root,
                env=os.environ
                | {
                    "BRIDGE_PORT": "54168",
                    "HOME": temporary_directory,
                    "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
                    "PYTHON": str(unsupported_python),
                },
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python executable must satisfy the project's Python requirement", result.stderr)

    def test_restart_uses_npm_ci_with_lockfile_and_normalizes_tauri_ci(self) -> None:
        self.assert_npm_install_command(package_lock=True, expected_command="ci")

    def test_restart_uses_npm_install_without_lockfile(self) -> None:
        self.assert_npm_install_command(package_lock=False, expected_command="install")

    def assert_npm_install_command(self, *, package_lock: bool, expected_command: str) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_dir = Path(temporary_directory)
            repo_root, desktop_dir, restart_script = self.make_script_fixture(temp_dir)
            if package_lock:
                (desktop_dir / "package-lock.json").write_text("{}\n", encoding="utf-8")

            bin_dir = temp_dir / "bin"
            bin_dir.mkdir()
            npm_log = temp_dir / "npm.log"
            npm = bin_dir / "npm"
            npm.write_text(f'#!/bin/sh\nprintf "CI=%s %s\\n" "$CI" "$*" >> "{npm_log}"\n', encoding="utf-8")
            npm.chmod(0o755)
            python = bin_dir / "python3.11"
            write_fake_python(python, "3.11", "26.0.1")
            for command in ("curl", "lsof", "ps"):
                executable = bin_dir / command
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o755)

            result = subprocess.run(
                [
                    "/bin/bash",
                    str(restart_script),
                    "--skip-python-build",
                    "--skip-install",
                    "--no-start",
                ],
                cwd=repo_root,
                env=os.environ
                | {
                    "BRIDGE_PORT": "54168",
                    "CI": "1",
                    "HOME": temporary_directory,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "PYTHON": str(python),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            npm_output = npm_log.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(npm_output, f"CI=1 {expected_command}\nCI=true run tauri -- build\n")


if __name__ == "__main__":
    unittest.main()
