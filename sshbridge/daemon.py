"""Deprecated compatibility entry point for the Unix-socket broker."""

import argparse
import sys

from .broker import serve
from .config import load_config
from .errors import BridgeError


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sshbridge-daemon")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--config", default="bridge.json")
    parser.add_argument("--profile")
    # Accepted only so old launch scripts fail safely instead of binding TCP.
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    arguments = parser.parse_args(argv)
    if not arguments.serve:
        parser.error("--serve is required")
    try:
        config = load_config(arguments.config)
        profile_name = arguments.profile or config["default_profile"]
        return serve(arguments.config, profile_name)
    except BridgeError as error:
        print(
            "daemon compatibility error [%s]: %s"
            % (error.code, error.message),
            file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
