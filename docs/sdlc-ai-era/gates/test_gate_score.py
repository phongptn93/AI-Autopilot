"""Kiem thu bo tinh diem cong. Chay: python -m unittest test_gate_score -v"""

import unittest
from pathlib import Path

from gate_score import BLOCK, CONFIRM, PASS, REVIEW, load_scorecard, score_gate

CARD = load_scorecard(Path(__file__).with_name("gate-scorecard.json"))


def all_status(gate: str, tier: str, status: str, evidence: str = "e") -> dict:
    out = {}
    for c in CARD["gates"][gate]["criteria"]:
        if tier in c.get("applies", list(CARD["tiers"])):
            out[c["id"]] = {"status": status, "evidence": evidence}
    return out


class WeightTests(unittest.TestCase):
    def test_weights_sum_to_100_per_gate(self):
        for g, cfg in CARD["gates"].items():
            self.assertEqual(sum(c["weight"] for c in cfg["criteria"]), 100, g)

    def test_weights_sum_for_applicable_set_is_positive(self):
        for tier in CARD["tiers"]:
            for g, cfg in CARD["gates"].items():
                app = [c for c in cfg["criteria"] if tier in c.get("applies", list(CARD["tiers"]))]
                self.assertTrue(app, f"{g}/{tier} khong co tieu chi nao")


class OutcomeTests(unittest.TestCase):
    def test_green_g3_all_pass_is_auto_pass(self):
        r = score_gate(CARD, "G3", "green", all_status("G3", "green", "pass"))
        self.assertEqual((r.score, r.outcome), (100, PASS))

    def test_green_g1_all_pass_needs_confirm(self):
        r = score_gate(CARD, "G1", "green", all_status("G1", "green", "pass"))
        self.assertEqual((r.score, r.outcome), (100, CONFIRM))

    def test_yellow_full_stage_never_passes_unattended(self):
        r = score_gate(CARD, "G4", "yellow", all_status("G4", "yellow", "pass"))
        self.assertEqual(r.outcome, REVIEW)

    def test_red_never_auto_even_at_100(self):
        for g in CARD["gates"]:
            r = score_gate(CARD, g, "red", all_status(g, "red", "pass"))
            self.assertEqual(r.score, 100)
            self.assertEqual(r.outcome, REVIEW, g)

    def test_hard_fail_blocks_regardless_of_score(self):
        # G1: bo ac_verifiable (30) -> diem 70, du de REVIEW neu khong co hard-fail.
        c = all_status("G1", "green", "pass")
        c["ac_verifiable"] = {"status": "fail"}
        r = score_gate(CARD, "G1", "green", c)
        self.assertEqual(r.score, 70)
        self.assertGreaterEqual(r.score, CARD["tiers"]["green"]["review_min"])
        self.assertEqual(r.outcome, BLOCK)
        self.assertIn("ac_verifiable", r.hard_fails)

    def test_failed_test_case_blocks_g4(self):
        c = all_status("G4", "green", "pass")
        c["tc_pass"] = {"status": "fail"}
        r = score_gate(CARD, "G4", "green", c)
        self.assertEqual(r.outcome, BLOCK)
        self.assertIn("tc_pass", r.hard_fails)

    def test_non_hard_fail_only_lowers_score(self):
        c = all_status("G1", "green", "pass")
        c["open_questions"] = {"status": "fail"}
        r = score_gate(CARD, "G1", "green", c)
        self.assertEqual(r.hard_fails, [])
        self.assertEqual(r.score, 85)

    def test_low_score_blocks(self):
        r = score_gate(CARD, "G3", "green", all_status("G3", "green", "partial"))
        self.assertEqual(r.score, 50)
        self.assertEqual(r.outcome, BLOCK)


class EvidenceTests(unittest.TestCase):
    def test_pass_without_evidence_is_downgraded_to_unknown(self):
        c = all_status("G3", "green", "pass", evidence="")
        r = score_gate(CARD, "G3", "green", c)
        self.assertEqual(r.score, 50)          # unknown = 0.5
        self.assertTrue(any("thiếu bằng chứng" in x for x in r.reasons))
        self.assertNotEqual(r.outcome, PASS)

    def test_missing_input_is_unknown_not_full_marks(self):
        r = score_gate(CARD, "G3", "green", {})
        self.assertEqual(r.score, 50)
        self.assertEqual(r.outcome, BLOCK)

    def test_fail_needs_no_evidence(self):
        c = all_status("G3", "green", "pass")
        c["secrets_clean"] = {"status": "fail"}
        r = score_gate(CARD, "G3", "green", c)
        self.assertEqual(r.outcome, BLOCK)


class NotApplicableTests(unittest.TestCase):
    def test_na_is_excluded_and_weights_renormalised(self):
        c = all_status("G3", "green", "pass")
        c["sandbox_least_priv"] = {"status": "na"}
        r = score_gate(CARD, "G3", "green", c)
        self.assertEqual(r.score, 100)

    def test_tier_applicability_filters_criteria(self):
        green = {c["id"] for c in CARD["gates"]["G2"]["criteria"] if "green" in c.get("applies", ["green"])}
        self.assertNotIn("sa_review", green)
        r = score_gate(CARD, "G2", "green", all_status("G2", "green", "pass"))
        self.assertNotIn("sa_review", r.components)


class ValidationTests(unittest.TestCase):
    def test_bad_status_rejected(self):
        with self.assertRaises(ValueError):
            score_gate(CARD, "G1", "green", {"ac_verifiable": {"status": "great", "evidence": "x"}})

    def test_unknown_gate_and_tier_rejected(self):
        with self.assertRaises(ValueError):
            score_gate(CARD, "G9", "green", {})
        with self.assertRaises(ValueError):
            score_gate(CARD, "G1", "purple", {})


if __name__ == "__main__":
    unittest.main()
