import os
import tempfile
import unittest

from config_sync import fsops


class FsopsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def p(self, *rel):
        return os.path.join(self.root, *rel)

    def write(self, rel, data=b"data"):
        path = self.p(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_safe_copy_file_creates_parents_and_refuses_overwrite(self):
        src = self.write("src.txt", b"hello")
        dst = self.p("nested/dir/out.txt")  # parents do not exist yet
        fsops.safe_copy(src, dst)
        self.assertTrue(os.path.isfile(dst))
        self.assertTrue(os.path.isfile(src))  # copy, not move
        with self.assertRaises(fsops.FsError):
            fsops.safe_copy(src, dst)  # dst now exists

    def test_safe_copy_tree_preserves_structure(self):
        self.write("tree/a.txt", b"a")
        self.write("tree/sub/b.txt", b"b")
        fsops.safe_copy(self.p("tree"), self.p("copy"))
        self.assertTrue(os.path.isfile(self.p("copy/a.txt")))
        self.assertTrue(os.path.isfile(self.p("copy/sub/b.txt")))

    def test_safe_move_refuses_overwrite(self):
        src = self.write("m-src.txt")
        dst = self.write("m-dst.txt")
        with self.assertRaises(fsops.FsError):
            fsops.safe_move(src, dst)
        self.assertTrue(os.path.isfile(src))  # untouched on refusal

    def test_symlink_create_and_remove_guards(self):
        target = self.write("target.txt")
        link = self.p("link")
        fsops.safe_symlink(target, link)
        self.assertTrue(os.path.islink(link))
        with self.assertRaises(fsops.FsError):
            fsops.safe_symlink(target, link)  # exists
        fsops.remove_symlink(link)
        self.assertFalse(os.path.lexists(link))
        with self.assertRaises(fsops.FsError):
            fsops.remove_symlink(target)  # never remove a real file
        self.assertTrue(os.path.isfile(target))

    def test_backup_restore_round_trip(self):
        home = self.p("home")
        orig = self.write("home/.config/app/config", b"cfg")
        backups = self.p("backups")
        bpath = fsops.backup(orig, backups, home)
        self.assertFalse(os.path.lexists(orig))          # moved aside
        self.assertTrue(os.path.isfile(bpath))
        self.assertTrue(bpath.startswith(backups + os.sep))
        fsops.restore(bpath, orig)
        self.assertTrue(os.path.isfile(orig))            # back in place
        self.assertFalse(os.path.lexists(bpath))

    def test_backup_refuses_path_outside_home(self):
        home = self.p("home")
        os.makedirs(home)
        outside = self.write("elsewhere.txt")
        with self.assertRaises(fsops.FsError):
            fsops.backup(outside, self.p("backups"), home)



if __name__ == "__main__":
    unittest.main()
