"""两个 JUnit/实际 pytest 流程；模型固定，不访问网络。"""

import unittest

from codex.verify_junit_ci import verify_guards, verify_workflow


class JunitCiTests(unittest.TestCase):
    def test_actual_failed_fixed_pytest_xml_mapping_and_ci_pipeline(self):
        self.assertEqual(verify_workflow()["status"], "passed")

    def test_supported_xml_subset_source_changes_and_rejections(self):
        self.assertEqual(verify_guards()["status"], "passed")


if __name__ == "__main__":
    unittest.main()
