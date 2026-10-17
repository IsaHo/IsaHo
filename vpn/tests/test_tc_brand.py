import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('tc_brand', Path(__file__).resolve().parents[1] / 'tc-panel' / 'brand.py')
brand = importlib.util.module_from_spec(spec)
spec.loader.exec_module(brand)


class BrandingTests(unittest.TestCase):
    def fixture(self, root):
        for name, replacements in brand.REPLACEMENTS.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('\n'.join(before for before, after in replacements))
        identity = root / 'internal/config/name'
        identity.parent.mkdir(parents=True)
        identity.write_text('x-ui')
        (root / 'LICENSE').write_text('upstream license must remain intact')

    def test_overlay_keeps_database_identity_and_license(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            with mock.patch.object(brand.subprocess, 'check_output', return_value=brand.UPSTREAM_COMMIT):
                result = brand.apply(root)
            self.assertEqual(result['backend_changes'], 0)
            self.assertEqual((root / 'internal/config/name').read_text(), 'x-ui')
            self.assertEqual((root / 'LICENSE').read_text(), 'upstream license must remain intact')
            self.assertIn('TC VPN', (root / 'frontend/login.html').read_text())

    def test_wrong_version_is_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            with mock.patch.object(brand.subprocess, 'check_output', return_value='wrong-version'):
                with self.assertRaises(ValueError):
                    brand.apply(root)
            self.assertIn('3X-UI', (root / 'frontend/src/pages/login/LoginPage.tsx').read_text())

    def test_missing_anchor_is_rejected_before_any_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            (root / 'frontend/login.html').write_text('unknown template')
            with mock.patch.object(brand.subprocess, 'check_output', return_value=brand.UPSTREAM_COMMIT):
                with self.assertRaises(ValueError):
                    brand.apply(root)
            self.assertIn('3X-UI', (root / 'frontend/src/pages/login/LoginPage.tsx').read_text())


if __name__ == '__main__':
    unittest.main()
