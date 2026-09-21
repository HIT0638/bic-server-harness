"""Build the unsigned Remote Explorer macOS application with py2app."""

import os
import sys
from pathlib import Path

from setuptools import setup

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

ASSET_DIR = PROJECT_ROOT / "sshbridge" / "web_assets"
ASSETS = [
    str(ASSET_DIR / name)
    for name in ("index.html", "app.js", "styles.css")
]

OPTIONS = {
    "argv_emulation": False,
    "strip": True,
    "packages": ["sshbridge"],
    "includes": ["Foundation", "WebKit", "webview"],
    "excludes": [
        "PyQt5", "PyQt6", "PySide2", "PySide6",
        "pytest", "tests",
    ],
    "extra_scripts": [str(HERE / "sshbridge_broker.py")],
    "plist": {
        "CFBundleName": "Remote Explorer",
        "CFBundleDisplayName": "Remote Explorer",
        "CFBundleIdentifier": "com.sshbridge.remote-explorer",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "LSApplicationCategoryType": "public.app-category.developer-tools",
        "NSHighResolutionCapable": True,
    },
}

os.chdir(HERE)

setup(
    name="Remote Explorer",
    version="0.1.0",
    app=[str(HERE / "desktop_app.py")],
    data_files=[("web_assets", ASSETS)],
    options={"py2app": OPTIONS},
)
