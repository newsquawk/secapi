"""
tests/test_characterization.py

Automated characterization test suite comparing current query output
against the pre-refactor baseline snapshot in tests/snapshots/baseline_current.json.
Uses standard library unittest so it runs in any Python environment without extra packages.
"""

import os
import unittest
from scripts.characterize_outputs import verify_diff, SNAPSHOT_PATH


class Test13FCharacterization(unittest.TestCase):
    def test_baseline_snapshot_exists(self):
        self.assertTrue(
            os.path.exists(SNAPSHOT_PATH),
            f"Baseline snapshot not found at {SNAPSHOT_PATH}",
        )

    def test_13f_output_characterization(self):
        """
        Runs the characterization verification engine.
        Ensures:
        1. Standard post-2023 stocks remain 100% stable.
        2. Internal price/value consistency holds across all holdings.
        3. Normalization logic properly fixes pre-2023 and sub-$1 penny stock values.
        4. Downstream fields weight_pct, value_pct, and form_type are present.
        """
        self.assertTrue(
            verify_diff(SNAPSHOT_PATH),
            "Characterization diff verification failed",
        )


if __name__ == "__main__":
    unittest.main()
