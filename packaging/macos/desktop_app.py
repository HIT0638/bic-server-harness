#!/usr/bin/env python3
"""py2app entry point for Remote Explorer."""

import sys

from sshbridge.desktop import app_main


if __name__ == "__main__":
    sys.exit(app_main())
