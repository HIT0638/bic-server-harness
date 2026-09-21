import unittest

from sshbridge import sftp_proto as P


class TestPrimitives(unittest.TestCase):
    def test_pstr_roundtrip(self):
        r = P.Reader(P.pstr("héllo"))
        self.assertEqual(r.string().decode("utf-8"), "héllo")

    def test_pstr_bytes(self):
        r = P.Reader(P.pstr(b"\x00\x01\xff"))
        self.assertEqual(r.string(), b"\x00\x01\xff")

    def test_u32_u64(self):
        self.assertEqual(P.Reader(P.u32(7)).u32(), 7)
        self.assertEqual(P.Reader(P.u64(1 << 40)).u64(), 1 << 40)

    def test_truncated_raises(self):
        with self.assertRaises(ValueError):
            P.Reader(b"\x00\x00").u32()


class TestAttrs(unittest.TestCase):
    def test_full_attrs(self):
        payload = (P.u32(0x0F) + P.u64(1234) + P.u32(10) + P.u32(20)
                   + P.u32(0o100644) + P.u32(111) + P.u32(222))
        a = P.Reader(payload).attrs()
        self.assertEqual(a["size"], 1234)
        self.assertEqual(a["uid"], 10)
        self.assertEqual(a["gid"], 20)
        self.assertEqual(a["perms"], 0o100644)
        self.assertEqual(a["atime"], 111)
        self.assertEqual(a["mtime"], 222)

    def test_perms_only(self):
        a = P.Reader(P.attrs_perms_only(0o040755)).attrs()
        self.assertEqual(a["perms"], 0o040755)
        self.assertNotIn("size", a)

    def test_extended_ignored(self):
        payload = (P.u32(P.ATTR_EXTENDED | P.ATTR_PERMISSIONS) + P.u32(0o644)
                   + P.u32(1) + P.pstr("k") + P.pstr("v"))
        a = P.Reader(payload).attrs()
        self.assertEqual(a["perms"], 0o644)


class TestFileType(unittest.TestCase):
    def test_types(self):
        self.assertEqual(P.file_type(0o040755), "dir")
        self.assertEqual(P.file_type(0o100644), "file")
        self.assertEqual(P.file_type(0o120777), "symlink")
        self.assertEqual(P.file_type(0o060000), "other")
        self.assertEqual(P.file_type(None), "other")


if __name__ == "__main__":
    unittest.main()
