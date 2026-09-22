"""Run the portable Windows baseline without real profiles or SSH connections."""

import argparse
import compileall
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Full discovery still includes POSIX Broker/sshd tests. Keep this list explicit
# until those tests have native Windows implementations.
TEST_MODULES = (
    "tests.test_paths",
    "tests.test_proto",
    "tests.test_exec_client",
    "tests.test_native_process_jobs",
    "tests.test_mcp_server",
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-windows", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / ".test-runtime/windows-ci")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "ssh_executable": shutil.which("ssh"),
        "suite": list(TEST_MODULES),
        "not_verified": [
            "Native Windows Broker (named pipe, ACL, singleton, shared clients)",
            "OpenSSH ControlMaster runtime support and connection reuse",
            "Windows MCP Host over stdio with a real Broker",
            "Windows-to-Linux SFTP, remote exec and reconnect",
            "Rsync and desktop packaging",
        ],
    }
    errors = []
    if args.require_windows and os.name != "nt":
        errors.append("This CI job requires native Windows.")
    ssh = report["ssh_executable"]
    if ssh is None:
        errors.append("OpenSSH client is missing.")
    else:
        try:
            version = subprocess.run(
                [ssh, "-V"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10)
            report["ssh_version"] = (version.stdout + version.stderr).strip()
            if version.returncode:
                errors.append("OpenSSH version probe failed.")
        except (OSError, subprocess.TimeoutExpired) as error:
            errors.append("OpenSSH version probe failed: %s" % error)
    try:
        # Tests historically skip any SDK ImportError. Make broken installations
        # a hard failure here, including native dependency loading errors.
        from sshbridge.mcp_server import _load_mcp_sdk
        _load_mcp_sdk()
        from mcp import Client, types  # noqa: F401
        from importlib.metadata import version
        report["mcp_version"] = version("mcp")
    except Exception as error:
        errors.append("MCP SDK preflight failed: %s: %s" % (type(error).__name__, error))

    if not errors:
        with (output / "tests.log").open("w", encoding="utf-8") as log:
            suite = unittest.defaultTestLoader.loadTestsFromNames(TEST_MODULES)
            result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
        report["tests"] = {
            "run": result.testsRun, "failures": len(result.failures),
            "errors": len(result.errors), "skipped": len(result.skipped),
        }
        print((output / "tests.log").read_text(encoding="utf-8"))
        if not result.wasSuccessful() or result.skipped or not result.testsRun:
            errors.append("Baseline requires passing tests with no skips.")
        compiled = compileall.compile_dir(str(ROOT / "sshbridge"), quiet=1)
        compiled = compileall.compile_file(str(ROOT / "remote.py"), quiet=1) and compiled
        if not compiled:
            errors.append("Compilation failed.")
    report["errors"] = errors
    report["passed"] = not errors
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = [
        "# Windows compatibility baseline", "",
        "Result: **%s**" % ("PASS" if not errors else "FAIL"),
        "", "Platform: %s; Python %s." % (report["platform"], report["python"]),
        "", "This is a compatibility baseline, **not Windows product readiness**.",
        "MCP tests use the real SDK with a fake Broker; process tests use local Python children.",
        "No user SSH configuration, real profile, remote host or credentials are used.",
        "", "## Not yet verified", "",
    ]
    summary.extend("- " + item for item in report["not_verified"])
    if "tests" in report:
        summary.extend(["", "Test counts: `%s`" % json.dumps(report["tests"])])
    if errors:
        summary.extend(["", "## Failures", ""] + ["- " + error for error in errors])
    (output / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n".join(summary))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
