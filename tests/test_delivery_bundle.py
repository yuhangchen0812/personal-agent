import unittest

from tests.support.verify_delivery_bundle import verify_workflow


class DeliveryBundleTests(unittest.TestCase):
    def test_extracted_runtime_demo_and_artifact_checks(self):
        self.assertEqual(verify_workflow()["status"], "passed")
