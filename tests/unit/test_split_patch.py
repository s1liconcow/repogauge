import unittest

from repogauge.export.split_patch import PatchSplitError, split_prod_and_test


class TestSplitPatch(unittest.TestCase):
    def test_split_patch_keeps_prod_and_test_file_chunks(self) -> None:
        diff = "\n".join(
            [
                "diff --git a/src/core.py b/src/core.py",
                "@@ -1 +1 @@",
                "-print('prod_old')",
                "+print('prod_new')",
                "diff --git a/tests/test_core.py b/tests/test_core.py",
                "@@ -1 +1 @@",
                "-assert old",
                "+assert new",
            ]
        )

        prod_patch, test_patch, _meta = split_prod_and_test(diff)
        self.assertIn("src/core.py", prod_patch)
        self.assertIn("tests/test_core.py", test_patch)
        self.assertNotIn("tests/test_core.py", prod_patch)
        self.assertNotIn("src/core.py", test_patch)

    def test_split_patch_includes_test_support_for_test_changes(self) -> None:
        diff = "\n".join(
            [
                "diff --git a/tests/conftest.py b/tests/conftest.py",
                "@@ -1 +1 @@",
                "-fixture_old",
                "+fixture_new",
                "diff --git a/tests/test_feature.py b/tests/test_feature.py",
                "@@ -1 +1 @@",
                "-def test_feature(): pass",
                "+def test_feature(): pass",
                "diff --git a/tests/fixtures/input.json b/tests/fixtures/input.json",
                "@@ -1 +1 @@",
                '-"{\\"k\\": 1}"',
                '+"{\\"k\\": 2}"',
            ]
        )

        prod_patch, test_patch, meta = split_prod_and_test(diff)
        self.assertFalse(prod_patch)
        self.assertIn("tests/conftest.py", test_patch)
        self.assertIn("tests/test_feature.py", test_patch)
        self.assertIn("tests/fixtures/input.json", test_patch)
        self.assertIn("tests/fixtures/input.json", meta["test_files"])

    def test_split_patch_keeps_test_support_with_no_tests_in_prod(self) -> None:
        diff = "\n".join(
            [
                "diff --git a/src/core.py b/src/core.py",
                "@@ -1 +1 @@",
                "-x = 1",
                "+x = 2",
                "diff --git a/tests/fixtures/input.json b/tests/fixtures/input.json",
                "@@ -1 +1 @@",
                '-"{\\"a\\": 1}"',
                '+"{\\"a\\": 2}"',
            ]
        )

        prod_patch, test_patch, _meta = split_prod_and_test(diff)
        self.assertIn("src/core.py", prod_patch)
        self.assertIn("tests/fixtures/input.json", prod_patch)
        self.assertNotIn("tests/fixtures/input.json", test_patch)
        self.assertEqual(test_patch, "")

    def test_split_patch_rejects_prod_test_rename(self) -> None:
        diff = "\n".join(
            [
                "diff --git a/src/core.py b/tests/test_core.py",
                "similarity index 100%",
                "rename from src/core.py",
                "rename to tests/test_core.py",
                "@@ -1 +1 @@",
                "-old",
                "+new",
            ]
        )
        with self.assertRaises(PatchSplitError):
            split_prod_and_test(diff)


if __name__ == "__main__":
    unittest.main()
