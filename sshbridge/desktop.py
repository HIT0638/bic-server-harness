"""macOS desktop shell for the local Remote Explorer."""

import argparse
import html
import os
import sys
import threading

from .config import Profile, load_config
from .errors import BridgeError
from .web import create_server, explorer_url

APP_NAME = "Remote Explorer"
DEFAULT_WIDTH = 1180
DEFAULT_HEIGHT = 760
MIN_WIDTH = 900
MIN_HEIGHT = 560
_APP_CONFIG = os.path.join(
    "Library", "Application Support", "SSHBridge", "bridge.json")


def resolve_desktop_config_path(
        explicit=None, environ=None, home=None, frozen=None):
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    environ = os.environ if environ is None else environ
    configured = environ.get("SSHBRIDGE_CONFIG")
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        home = os.path.expanduser("~") if home is None else home
        return os.path.abspath(os.path.join(home, _APP_CONFIG))
    return os.path.abspath("bridge.json")


def run_desktop(profile, config_path, webview_module=None,
                server_factory=create_server):
    if sys.platform != "darwin":
        raise BridgeError(
            "DESKTOP_UNSUPPORTED",
            "desktop mode is currently supported only on macOS")
    if profile.connection_policy["mode"] != "broker":
        raise BridgeError(
            "BROKER_UNSUPPORTED",
            "desktop mode requires connection_policy.mode=broker")
    webview_module = webview_module or _load_webview()
    server = server_factory(
        profile, config_path=config_path, port=0)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.2},
        name="sshbridge-desktop-http")
    thread.start()
    failure = None
    try:
        window = webview_module.create_window(
            "%s - %s" % (APP_NAME, profile.name),
            explorer_url(server),
            width=DEFAULT_WIDTH,
            height=DEFAULT_HEIGHT,
            min_size=(MIN_WIDTH, MIN_HEIGHT),
            resizable=True,
            zoomable=False,
            draggable=False)
        window.events.closing += _confirm_close
        webview_module.start(private_mode=True)
    except BaseException as error:
        failure = error

    cleanup_error = _close_server(server, thread)
    if failure is not None:
        if isinstance(failure, (BridgeError, KeyboardInterrupt, SystemExit)):
            raise failure
        raise BridgeError(
            "DESKTOP_START_FAILED",
            "cannot start desktop window: %s" % failure)
    if cleanup_error is not None:
        raise cleanup_error
    return 0


def app_main(argv=None):
    parser = argparse.ArgumentParser(prog=APP_NAME)
    parser.add_argument("--config", metavar="FILE")
    parser.add_argument("--profile", metavar="NAME")
    arguments = parser.parse_args(argv)
    try:
        config_path = resolve_desktop_config_path(arguments.config)
        config = load_config(config_path)
        profile_name = arguments.profile or config["default_profile"]
        if profile_name not in config["profiles"]:
            raise BridgeError(
                "INVALID_CONFIG",
                "unknown profile: %s (have: %s)"
                % (profile_name, ", ".join(config["profiles"])))
        profile = Profile(profile_name, config["profiles"][profile_name])
        return run_desktop(profile, config["file"])
    except BridgeError as error:
        print(
            "desktop error [%s]: %s" % (error.code, error.message),
            file=sys.stderr)
        _show_startup_error(error)
        return 1


def _load_webview():
    try:
        import webview
    except ImportError as error:
        raise BridgeError(
            "DESKTOP_DEPENDENCY_MISSING",
            "desktop mode requires pywebview and macOS PyObjC dependencies: %s"
            % error)
    return webview


def _confirm_close(window):
    try:
        dirty = bool(window.run_js(
            "document.documentElement.dataset.dirty === 'true'"))
    except Exception:
        return True
    if not dirty:
        return True
    return bool(window.create_confirmation_dialog(
        "Unsaved changes",
        "The current file has unsaved changes. Close the window anyway?"))


def _close_server(server, thread):
    problems = []
    try:
        if thread.is_alive():
            server.shutdown()
    except Exception as error:
        problems.append("shutdown: %s" % error)
    try:
        server.server_close()
    except Exception as error:
        problems.append("server_close: %s" % error)
    thread.join(timeout=5)
    if thread.is_alive():
        problems.append("HTTP server thread did not stop")
    if problems:
        return BridgeError(
            "DESKTOP_START_FAILED",
            "desktop cleanup failed: %s" % "; ".join(problems))
    return None


def _show_startup_error(error):
    if sys.platform != "darwin":
        return
    try:
        webview = _load_webview()
        title = "%s failed to start" % APP_NAME
        webview.create_window(
            title, html=_startup_error_html(error), width=640, height=320,
            min_size=(480, 240), resizable=True)
        webview.start(private_mode=True)
    except Exception:
        pass


def _startup_error_html(error):
    title = "%s failed to start" % APP_NAME
    message = html.escape("[%s] %s" % (error.code, error.message))
    return (
        "<main style=\"font:14px -apple-system;padding:24px;"
        "color:#171717\"><h1 style=\"font-size:18px\">%s</h1>"
        "<pre style=\"white-space:pre-wrap\">%s</pre></main>"
        % (html.escape(title), message))


if __name__ == "__main__":
    sys.exit(app_main())
