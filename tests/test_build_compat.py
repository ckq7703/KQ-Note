import glob
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import xor_obfuscate  # noqa: E402


class BuildCompatTest(unittest.TestCase):
    """CI runs xor_obfuscate.py over every .py file before Nuitka compiles them; it injects a
    helper after the last top-level import line. A file that breaks under that step breaks
    the release build. Known triggers: a multi-line `from x import (` as the last import, and
    an import line ending in the word "import" (e.g. a module named legacy_import), because the
    script's regex lets `\\s+` swallow the newline and the next line."""

    def test_every_client_module_still_compiles_after_the_obfuscation_step(self):
        sources = [os.path.join(ROOT, "main.py")] + sorted(glob.glob(os.path.join(ROOT, "app", "**", "*.py"), recursive=True))
        self.assertGreater(len(sources), 10)
        with tempfile.TemporaryDirectory() as tmp:
            for src in sources:
                copy = os.path.join(tmp, os.path.basename(src))
                shutil.copy(src, copy)
                xor_obfuscate.obfuscate_string_constants_in_file(copy)
                with open(copy, encoding="utf-8") as f:
                    try:
                        compile(f.read(), src, "exec")
                    except SyntaxError as e:
                        self.fail(f"{os.path.relpath(src, ROOT)} breaks after the XOR step: {e}")


if __name__ == "__main__":
    unittest.main()
