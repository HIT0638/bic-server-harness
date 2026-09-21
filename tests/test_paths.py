import unittest

from sshbridge.errors import BridgeError
from sshbridge.paths import (basename_of, ensure_within_root, join,
                             normalize_root, parent_of, resolve_virtual)

ROOT = "/home/user/tengbo/bicservertest"


class TestNormalizeRoot(unittest.TestCase):
    def test_strips_trailing_slash(self):
        self.assertEqual(normalize_root("/a/b/"), "/a/b")

    def test_rejects_relative(self):
        with self.assertRaises(BridgeError):
            normalize_root("home/user")

    def test_rejects_server_root(self):
        with self.assertRaises(BridgeError):
            normalize_root("/")

    def test_rejects_dotdot(self):
        with self.assertRaises(BridgeError):
            normalize_root("/a/../b")

    def test_rejects_empty(self):
        with self.assertRaises(BridgeError):
            normalize_root("")


class TestResolveVirtual(unittest.TestCase):
    def test_root(self):
        self.assertEqual(resolve_virtual("/", ROOT), ROOT)

    def test_relative(self):
        self.assertEqual(resolve_virtual("src", ROOT), ROOT + "/src")

    def test_absolute_maps_into_root(self):
        self.assertEqual(resolve_virtual("/src/main.py", ROOT),
                         ROOT + "/src/main.py")

    def test_backslash_normalized(self):
        self.assertEqual(resolve_virtual("src\\main.py", ROOT),
                         ROOT + "/src/main.py")

    def test_dot_segments(self):
        self.assertEqual(resolve_virtual("/./src/./x", ROOT), ROOT + "/src/x")

    def test_dotdot_clamps_at_root(self):
        self.assertEqual(resolve_virtual("/../../etc/passwd", ROOT),
                         ROOT + "/etc/passwd")

    def test_inner_dotdot(self):
        self.assertEqual(resolve_virtual("/a/b/../c", ROOT), ROOT + "/a/c")

    def test_empty_rejected(self):
        with self.assertRaises(BridgeError):
            resolve_virtual("", ROOT)

    def test_nul_rejected(self):
        with self.assertRaises(BridgeError):
            resolve_virtual("/a\x00b", ROOT)


class TestEnsureWithinRoot(unittest.TestCase):
    def test_root_itself(self):
        ensure_within_root(ROOT, ROOT)

    def test_child(self):
        ensure_within_root(ROOT + "/a/b", ROOT)

    def test_prefix_trick_blocked(self):
        with self.assertRaises(BridgeError):
            ensure_within_root(ROOT + "EVIL", ROOT)

    def test_symlink_escape_blocked(self):
        with self.assertRaises(BridgeError):
            ensure_within_root("/etc/passwd", ROOT)


class TestHelpers(unittest.TestCase):
    def test_parent_and_basename(self):
        self.assertEqual(parent_of(ROOT + "/a.txt"), ROOT)
        self.assertEqual(basename_of(ROOT + "/a.txt"), "a.txt")
        self.assertEqual(parent_of("/a"), "/")

    def test_join(self):
        self.assertEqual(join("/a", "b"), "/a/b")
        self.assertEqual(join("/a/", "b"), "/a/b")


if __name__ == "__main__":
    unittest.main()
