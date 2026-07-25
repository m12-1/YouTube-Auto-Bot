"""
tests/test_filesystem_security.py

Tests for the shared.path_utils security primitives to ensure
path traversal attempts are blocked.
"""

import unittest
from pathlib import Path
from shared.path_utils import safe_path, sanitize_filename, PROJECT_ROOT

class TestFilesystemSecurity(unittest.TestCase):

    def test_safe_path_valid(self):
        """Test that safe_path resolves valid paths within the project root."""
        result = safe_path(PROJECT_ROOT, "db", "test.json")
        self.assertTrue(result.is_absolute())
        self.assertTrue(result.is_relative_to(PROJECT_ROOT))
        self.assertEqual(result.name, "test.json")

    def test_safe_path_traversal_blocked(self):
        """Test that safe_path blocks path traversal (e.g. ../../etc/passwd)."""
        with self.assertRaises(ValueError):
            safe_path(PROJECT_ROOT, "output", "..", "..", "..", "Windows", "System32")
            
        with self.assertRaises(ValueError):
            safe_path(PROJECT_ROOT, "..", "outside.txt")

    def test_sanitize_filename(self):
        """Test that sanitize_filename removes dangerous characters."""
        self.assertEqual(sanitize_filename("valid_name.txt"), "valid_name.txt")
        self.assertEqual(sanitize_filename("invalid/name\\file.txt"), "invalid_name_file.txt")
        self.assertEqual(sanitize_filename("..name.."), "..name..")

if __name__ == "__main__":
    unittest.main()
