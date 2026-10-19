import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import rewrite_compile_commands as rewrite


class RewritePathValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.source.mkdir()
        self.target.mkdir()
        self.database = self.root / "compile_commands.json"
        self.original = json.dumps([{
            "directory": str(self.root),
            "file": str(self.source / "main.c"),
            "arguments": ["cc", "-I" + str(self.source / "include"),
                          "-c", str(self.source / "main.c")],
        }])
        self.database.write_text(self.original)

    def run_cli(self, *options):
        argv = ["rewrite_compile_commands.py", str(self.database),
                str(self.source), str(self.target), *options]
        with patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return rewrite.main()

    def assert_rewritten(self):
        entry = json.loads(self.database.read_text())[0]
        self.assertEqual(entry["file"], str(self.target / "main.c"))
        self.assertEqual(entry["arguments"], [
            "cc", "-I" + str(self.target / "include"),
            "-c", str(self.target / "main.c")])
        self.assertEqual(entry["directory"], str(self.root))

    def test_different_directories_rejected_by_default(self):
        with self.assertRaises(SystemExit) as error:
            self.run_cli()
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.database.read_text(), self.original)

    def test_explicit_different_tree_mapping(self):
        self.assertEqual(self.run_cli("--allow-different-tree"), 0)
        self.assert_rewritten()

    def test_symlink_works_without_override(self):
        self.target.rmdir()
        self.target.symlink_to(self.source, target_is_directory=True)
        self.assertEqual(self.run_cli(), 0)
        self.assert_rewritten()

    def test_missing_target_rejected_even_with_override(self):
        self.target.rmdir()
        with self.assertRaises(SystemExit) as error:
            self.run_cli("--allow-different-tree")
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.database.read_text(), self.original)


if __name__ == "__main__":
    unittest.main()
