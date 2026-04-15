import unittest

from repogauge.manifest import Manifest, ManifestStepStatus


class TestManifest(unittest.TestCase):
    def test_manifest_round_trip(self):
        manifest = Manifest.start("mine")
        manifest.mark_step("scan", ManifestStepStatus.SUCCEEDED)
        manifest.finish(status="succeeded")

        payload = manifest.to_dict()
        assert payload["command"] == "mine"
        assert payload["status"] == "succeeded"
        assert payload["step_statuses"]["scan"] == "succeeded"


if __name__ == "__main__":
    unittest.main()
