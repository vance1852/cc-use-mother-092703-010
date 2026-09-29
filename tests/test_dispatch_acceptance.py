from __future__ import annotations

import unittest
from pathlib import Path

from renewable_dispatch.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class AcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertEqual(result["plan_revision"], 2)
        self.assertEqual(result["frozen_slots"], 2)
        self.assertEqual(result["first_revision_total_unmet"], "0.000")
        self.assertEqual(result["second_revision_total_unmet"], "0.000")
        self.assertEqual(result["receipts"]["duplicate"], "accepted")
        self.assertEqual(result["receipts"]["late_double_settle"], "late")
        self.assertEqual(result["receipts"]["late_ack"], "late")
        self.assertGreater(result["recovered_pending_instructions"], 0)
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
