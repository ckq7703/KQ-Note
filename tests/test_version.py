import glob
import os
import re
import unittest

from tests.store_case import StoreCase  # noqa: F401  (puts the repo root on sys.path)
from app.version import __version__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class VersionTest(unittest.TestCase):
    """The version lives in several files that a release has to bump together."""

    def test_every_place_agrees_with_app_version(self):
        four = f"{__version__}.0"
        self.assertRegex(read("installer.iss"), rf'#define MyAppVersion "{re.escape(__version__)}"')
        info = read("version_info.txt")
        self.assertIn(f"u'{four}'", info)
        as_tuple = ", ".join(__version__.split(".")) + ", 0"
        self.assertIn(f"filevers=({as_tuple})", info)
        self.assertIn(f"prodvers=({as_tuple})", info)
        workflow = read(".github", "workflows", "release.yml")
        self.assertIn(f"--file-version={four}", workflow)
        self.assertIn(f"--product-version={four}", workflow)
        self.assertIn(f'v{__version__}"', workflow)  # the manual-run fallback tag


if __name__ == "__main__":
    unittest.main()
