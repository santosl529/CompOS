"""Smoke tests for S9 score. Run: .venv/bin/python -m unittest discover -s tests -v"""
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from s09_score import (  # noqa: E402
    compare, rollup, rank_candidates, qpl_listed, build_qpl_index, spec_key, candidate_id, delta_id,
    price_delta, baseline_prices, risk_rank, RISK_RANK, qualification_delta, qualification_gap,
    is_scored_class, SCORED_CLASSES, BURDEN_CLASSES,
    evaluate_hard_gate, hard_gates_failed, hard_gate_attrs, attr_soft_risk, composite,
    risk_from_composite, gate_type, CATEGORY_WEIGHTS, UNIVERSAL_WEIGHTS,
)


def req(name, op, num=None, lo=None, hi=None, txt="", status="number", uom=""):
    return {"requirement_name": name, "operator": op, "value_num": num, "value_min": lo, "value_max": hi,
            "value_text": txt, "parse_status": status, "uom": uom}


def cand(num=None, lo=None, hi=None, txt="", status="number", uom=""):
    return {"value_num": num, "value_min": lo, "value_max": hi, "value_text": txt, "parse_status": status, "uom": uom}


class TestCompare(unittest.TestCase):
    def v(self, *a, **k):
        return compare(*a, **k)[0]

    def test_gte_threshold(self):
        r = req("power_rating", "gte", 0.125, uom="W")
        self.assertEqual(self.v(r, cand(0.25)), "pass")
        self.assertEqual(self.v(r, cand(0.125)), "pass")
        self.assertEqual(self.v(r, cand(0.12)), "marginal")   # within 10%
        self.assertEqual(self.v(r, cand(0.1)), "fail")

    def test_lte_threshold(self):
        r = req("tolerance", "lte", 1.0, uom="%")
        self.assertEqual(self.v(r, cand(0.5)), "pass")
        self.assertEqual(self.v(r, cand(1.05)), "marginal")
        self.assertEqual(self.v(r, cand(5.0)), "fail")

    def test_negative_threshold_temp_min(self):
        r = req("operating_temp_min", "lte", -55.0, uom="degC")
        self.assertEqual(self.v(r, cand(-65.0)), "pass")
        self.assertEqual(self.v(r, cand(-55.0)), "pass")
        self.assertEqual(self.v(r, cand(-50.0)), "marginal")
        self.assertEqual(self.v(r, cand(-40.0)), "fail")

    def test_eq_with_pct_rule(self):
        r = req("resistance", "eq", 10_000.0, uom="ohm")
        self.assertEqual(self.v(r, cand(10_000.0), pct_rule=5.0), "pass")
        self.assertEqual(self.v(r, cand(10_400.0), pct_rule=5.0), "pass")
        self.assertEqual(self.v(r, cand(10_800.0), pct_rule=5.0), "marginal")
        self.assertEqual(self.v(r, cand(12_000.0), pct_rule=5.0), "fail")

    def test_unknown_never_pass(self):
        r = req("resistance", "eq", 10_000.0)
        self.assertEqual(self.v(r, None), "unknown")                                   # attribute missing
        self.assertEqual(self.v(r, cand(txt="10 furlongs", status="unparsed")), "unknown")
        self.assertEqual(self.v(req("resistance", "eq", None, txt="?", status="unparsed"), cand(10_000.0)), "unknown")

    def test_text_equality(self):
        r = req("package_case", "eq", txt="0603", status="text")
        self.assertEqual(self.v(r, cand(txt="0603", status="text")), "pass")
        self.assertEqual(self.v(r, cand(txt="0805", status="text")), "fail")
        self.assertEqual(self.v(r, cand(txt="", status="text")), "unknown")

    def test_candidate_range_against_threshold_uses_conservative_bound(self):
        r = req("operating_temp_max", "gte", 125.0)
        self.assertEqual(self.v(r, cand(lo=-55.0, hi=155.0, status="range")), "pass")
        self.assertEqual(self.v(r, cand(lo=-55.0, hi=85.0, status="range")), "fail")

    def test_range_requirement(self):
        r = req("operating_temp", "range", lo=-55.0, hi=125.0, status="range")
        self.assertEqual(self.v(r, cand(lo=-65.0, hi=150.0, status="range")), "pass")
        self.assertEqual(self.v(r, cand(lo=-40.0, hi=85.0, status="range")), "fail")
        self.assertEqual(self.v(r, cand(100.0)), "pass")

    def test_delta_has_display_and_bare_contract_values(self):
        d = compare(req("power_rating", "gte", 0.125, uom="W"), cand(0.1, uom="W"))
        self.assertEqual(d.verdict, "fail")
        self.assertEqual(d.required, ">= 0.125 W")
        self.assertEqual(d.candidate, "0.1 W")
        self.assertEqual(d.required_bare, "0.125")   # bare base-unit number for Foundry
        self.assertEqual(d.candidate_bare, "0.1")
        self.assertEqual(d.uom, "W")
        d = compare(req("package_case", "eq", txt="0603", status="text"), cand(txt="0805", status="text"))
        self.assertEqual((d.required_bare, d.candidate_bare), ("0603", "0805"))
        d = compare(req("resistance", "eq", 10_000.0, uom="ohm"), None)
        self.assertEqual((d.verdict, d.candidate_bare), ("unknown", ""))


class TestQualificationRequirement(unittest.TestCase):
    def test_qualification_delta_is_unknown_never_fail(self):
        # CHANGE-qualification-burden: not verifiable from commercial data ≠ does not meet
        r = req("qualification", "boolean", txt="true", status="boolean")
        self.assertEqual(qualification_delta(r, True).verdict, "unknown")
        d = qualification_delta(r, False)
        self.assertEqual(d.verdict, "unknown")
        self.assertEqual(d.candidate_bare, "NOT_QPL_LISTED")


class TestOperatorEnum(unittest.TestCase):
    """SCHEMA §8: operator is the closed set eq | gte | lte | range | in_set | boolean."""

    def test_in_set_membership_on_normalised_value(self):
        r = req("package_case", "in_set", txt="0603|0805", status="set")
        self.assertEqual(compare(r, cand(txt="0805", status="text"))[0], "pass")
        self.assertEqual(compare(r, cand(txt="1206", status="text"))[0], "fail")
        self.assertEqual(compare(r, cand(txt="", status="empty"))[0], "unknown")
        # numeric members compare against the candidate's base-unit number
        r = req("resistance", "in_set", txt="10000|22000", status="set")
        self.assertEqual(compare(r, cand(22_000.0))[0], "pass")
        self.assertEqual(compare(r, cand(47_000.0))[0], "fail")

    def test_boolean(self):
        r = req("rohs", "boolean", txt="true", status="boolean")
        self.assertEqual(compare(r, cand(txt="Yes", status="text"))[0], "pass")
        self.assertEqual(compare(r, cand(txt="No", status="text"))[0], "fail")
        self.assertEqual(compare(r, cand(txt="RoHS3", status="text"))[0], "unknown")  # not a truth token

    def test_legacy_symbols_still_understood_on_input(self):
        # hand-made files may say ">=": it is mapped, never silently treated as eq
        self.assertEqual(compare(req("power_rating", ">=", 0.125), cand(0.25))[0], "pass")
        self.assertEqual(compare(req("power_rating", ">=", 0.125), cand(0.05))[0], "fail")


class TestKeysAndPrices(unittest.TestCase):
    def test_surrogate_keys(self):
        cid = candidate_id("5905001234567", "CRCW060310K0FKEA")
        self.assertEqual(cid, "5905001234567__CRCW060310K0FKEA")
        self.assertEqual(delta_id(cid, "resistance"), "5905001234567__CRCW060310K0FKEA__resistance")

    def test_price_delta_null_when_either_side_missing(self):
        # SCHEMA §3: price_delta_indicative, never "savings"; null, never 0, when a side is missing
        self.assertEqual(price_delta(None, 0.5), "")
        self.assertEqual(price_delta(2.0, None), "")
        self.assertEqual(price_delta(2.0, 0.5), "1.5000")
        self.assertEqual(price_delta(0.5, 2.0), "-1.5000")  # a negative delta is real, not null

    def test_gov_price_uses_most_recent_year_with_a_price(self):
        ph = pd.DataFrame([
            {"nsn": "1", "fiscal_year": "2022", "unit_price_avg": "3.0", "quantity": "10"},
            {"nsn": "1", "fiscal_year": "2024", "unit_price_avg": "", "quantity": ""},      # null row must not win
            {"nsn": "1", "fiscal_year": "2023", "unit_price_avg": "2.5", "quantity": ""},   # price without quantity
            {"nsn": "2", "fiscal_year": "", "unit_price_avg": "", "quantity": ""},          # "no history" null row
        ])
        self.assertEqual(baseline_prices(ph), {"1": (2.5, None, 2023)})


class TestQualificationBurden(unittest.TestCase):
    """CHANGE-qualification-burden.md: risk is technical; qualification is a reported burden."""

    def test_scored_classes_are_the_measurable_three(self):
        self.assertEqual(SCORED_CLASSES, {"electrical", "mechanical", "environmental"})
        self.assertEqual(BURDEN_CLASSES, {"qualification", "traceability"})
        self.assertTrue(is_scored_class("electrical"))
        self.assertTrue(is_scored_class("mechanical"))
        self.assertTrue(is_scored_class("environmental"))
        self.assertFalse(is_scored_class("qualification"))
        self.assertFalse(is_scored_class("traceability"))

    def test_gap_is_a_property_of_the_baseline_profile(self):
        reqs = [
            {**req("resistance", "eq", 150), "requirement_class": "electrical"},
            {**req("qualification", "boolean", txt="true", status="boolean"), "requirement_class": "qualification"},
            {**req("lot_traceability", "boolean", txt="true", status="boolean"), "requirement_class": "traceability"},
            {**req("solderability_per_j_std_002", "boolean", txt="true", status="boolean"),
             "requirement_class": "qualification"},
        ]
        count, summary = qualification_gap(reqs)
        self.assertEqual(count, 3)
        self.assertEqual(summary, "lot_traceability|qualification|solderability_per_j_std_002")

    def test_low_does_not_require_qpl(self):
        v = [("resistance", "electrical", "pass")] * 3 + [("tolerance", "electrical", "marginal")]  # 25% marginal
        self.assertEqual(rollup(v, True, "qpl ok")[0], "low")
        self.assertEqual(rollup(v, False, "not listed")[0], "low")

    def test_qualification_and_traceability_are_excluded_from_counts_and_risk(self):
        # every measurable requirement passes; every qualification clause is unknown
        v = [("resistance", "electrical", "pass"), ("operating_temp_max", "environmental", "pass"),
             ("qualification", "qualification", "unknown"), ("lot_traceability", "traceability", "unknown")]
        risk, rationale, counts = rollup(v, False, "not listed", spec_ref="MIL-DTL-38999")
        self.assertEqual(risk, "low")
        self.assertEqual(counts, {"pass": 2, "fail": 0, "marginal": 0, "unknown": 0})
        self.assertNotIn("fails a qualification", rationale.lower())
        self.assertIn("measurable", rationale)
        self.assertIn("2 qualification", rationale)
        self.assertIn("MIL-DTL-38999", rationale)

    def test_rationale_names_measurable_fails_and_the_burden(self):
        v = [("resistance", "electrical", "pass"), ("operating_temp_min", "environmental", "fail"),
             ("qualification", "qualification", "unknown"), ("lot_traceability", "traceability", "unknown")]
        risk, rationale, counts = rollup(v, False, "not listed", spec_ref="MIL-DTL-38999")
        self.assertEqual(risk, "medium")
        self.assertEqual(counts["fail"], 1)
        self.assertIn("operating_temp_min", rationale)
        self.assertIn("2 qualification clauses under MIL-DTL-38999", rationale)
        self.assertNotRegex(rationale, r"(?i)fails qualification")


class TestRollup(unittest.TestCase):

    def test_more_than_two_fails_is_high(self):
        v = [(f"a{i}", "electrical", "fail") for i in range(3)] + [("b", "electrical", "pass")]
        self.assertEqual(rollup(v, True, "")[0], "high")

    def test_one_or_two_fails_is_medium(self):
        v = [("a", "electrical", "fail"), ("b", "electrical", "pass"), ("c", "electrical", "pass")]
        self.assertEqual(rollup(v, True, "")[0], "medium")
        v.append(("d", "environmental", "fail"))
        self.assertEqual(rollup(v, True, "")[0], "medium")

    def test_unknowns_are_a_risk_signal(self):
        v = [("a", "electrical", "pass"), ("b", "electrical", "unknown")]  # 50% unknown
        risk, rationale, counts = rollup(v, True, "")
        self.assertEqual(risk, "medium")
        self.assertIn("unknown", rationale)
        v = [("a", "electrical", "pass")] * 8 + [("b", "electrical", "unknown")] * 2  # 20% unknown
        self.assertEqual(rollup(v, True, "")[0], "low")

    def test_marginals_are_capped_too(self):
        # SCHEMA §10: eleven near-misses are not a low-risk part
        v = [("a", "electrical", "pass")] * 6 + [("b", "electrical", "marginal")] * 4  # 40% marginal
        risk, rationale, _ = rollup(v, True, "")
        self.assertEqual(risk, "medium")
        self.assertIn("marginal", rationale)
        v = [("a", "electrical", "pass")] * 7 + [("b", "electrical", "marginal")] * 3  # 30% is the limit, not over it
        self.assertEqual(rollup(v, True, "")[0], "low")

    def test_no_requirements_is_not_low(self):
        self.assertEqual(rollup([], True, "")[0], "medium")

    def test_second_pass_lifecycle(self):
        v = [("a", "electrical", "pass")]
        self.assertEqual(rollup(v, True, "", {"lifecycle_status": "obsolete", "distributor_count": "5"})[0], "high")
        self.assertEqual(rollup(v, True, "", {"lifecycle_status": "nrnd", "distributor_count": "5"})[0], "medium")
        self.assertEqual(rollup(v, True, "", {"lifecycle_status": "active", "distributor_count": "1"})[0], "medium")
        self.assertEqual(rollup(v, True, "", {"lifecycle_status": "active", "distributor_count": "4"})[0], "low")
        # enrichment can only raise risk, never lower it
        v_bad = [("a", "electrical", "fail")] * 3
        self.assertEqual(rollup(v_bad, True, "", {"lifecycle_status": "active", "distributor_count": "9"})[0], "high")

    def test_counts(self):
        v = [("a", "electrical", "pass"), ("b", "electrical", "fail"), ("c", "electrical", "marginal"),
             ("d", "electrical", "unknown")]
        _, _, counts = rollup(v, False, "")
        self.assertEqual(counts, {"pass": 1, "fail": 1, "marginal": 1, "unknown": 1})


class TestQpl(unittest.TestCase):
    def test_spec_key(self):
        self.assertEqual(spec_key("M55342K06B10E0R"), "55342")
        self.assertEqual(spec_key("MIL-PRF-55342/6"), "55342")
        self.assertEqual(spec_key(""), "")

    def test_listed_only_by_part_number_AND_manufacturer_or_cage(self):
        # SCHEMA §3/§6: the PAIR is what makes the join safe. Part number alone is forbidden (MPNs are not
        # unique across manufacturers); manufacturer alone is not a qualification of THIS part.
        qpl = pd.DataFrame([
            {"governing_spec": "MIL-PRF-55342", "manufacturer_name": "Vishay Dale", "cage_code": "91637",
             "qualified_part_number": "M55342K06B10E0R"},
        ])
        idx = build_qpl_index(qpl)
        pn = "M55342K06B10E0R"
        self.assertTrue(qpl_listed(idx, pn, "Vishay Dale", pn)[0])                       # pn + manufacturer
        self.assertTrue(qpl_listed(idx, pn, "Vishay", pn)[0])                            # manufacturer family name
        self.assertTrue(qpl_listed(idx, pn, "Someone Else", pn, cage="91637")[0])        # pn + CAGE
        self.assertFalse(qpl_listed(idx, pn, "Someone Else", pn)[0])                     # pn alone: forbidden
        self.assertFalse(qpl_listed(idx, pn, "Vishay Dale", "CRCW060310K0FKEA")[0])      # manufacturer alone: no
        self.assertFalse(qpl_listed(idx, pn, "Vishay Dale", pn.lower() + "x")[0])        # different part
        self.assertFalse(qpl_listed(idx, "", "Vishay Dale", pn)[0])                      # no governing spec
        self.assertFalse(qpl_listed({}, pn, "Vishay Dale", pn)[0])                       # no QPL data -> False
        self.assertFalse(qpl_listed(idx, "RWR80S1R00FR", "Vishay Dale", pn)[0])          # other spec's QPL


class TestRanking(unittest.TestCase):
    def test_rank_order(self):
        def row(mpn, risk, p, f, m, u, delta=""):
            return {"nsn": "1", "candidate_mpn": mpn, "risk_level": risk, "pass_count": p, "fail_count": f,
                    "marginal_count": m, "unknown_count": u, "price_delta_indicative": delta}
        sc = pd.DataFrame([row("C", "high", 9, 3, 0, 0), row("A", "low", 8, 0, 1, 0), row("B", "medium", 7, 1, 0, 0),
                           row("D", "medium", 9, 0, 0, 5)])
        ranked = rank_candidates(sc)
        order = list(ranked.sort_values("rank_within_nsn")["candidate_mpn"])
        self.assertEqual(order, ["A", "D", "B", "C"])  # D (0 fails, unknowns) beats B (1 fail)

    def test_price_never_crosses_a_risk_band(self):
        # SCHEMA §3: risk_rank leads; price_delta_indicative descending only WITHIN a band; nulls last in band
        def row(mpn, risk, delta):
            return {"nsn": "1", "candidate_mpn": mpn, "risk_level": risk, "pass_count": 5, "fail_count": 0,
                    "marginal_count": 0, "unknown_count": 0, "price_delta_indicative": delta}
        sc = pd.DataFrame([row("cheap_medium", "medium", "9.00"), row("low_null", "low", ""),
                           row("low_small", "low", "0.50"), row("low_big", "low", "2.00")])
        order = list(rank_candidates(sc).sort_values("rank_within_nsn")["candidate_mpn"])
        self.assertEqual(order, ["low_big", "low_small", "low_null", "cheap_medium"])

    def test_risk_rank_is_integer_low_medium_high(self):
        # PRD: risk_rank 1/2/3 for low/medium/high so Foundry can sort on it (alphabetical
        # ordering of the string values would yield high -> low -> medium).
        self.assertEqual(RISK_RANK, {"low": 1, "medium": 2, "high": 3, "unscored": 4})
        self.assertEqual(risk_rank("low"), 1)
        self.assertEqual(risk_rank("medium"), 2)
        self.assertEqual(risk_rank("high"), 3)
        self.assertEqual(risk_rank("unscored"), 4)
        with self.assertRaises(ValueError):
            risk_rank("unknown")
        # sorting on risk_rank ascending yields low -> medium -> high -> unscored
        levels = ["high", "unscored", "low", "medium"]
        self.assertEqual(sorted(levels, key=risk_rank), ["low", "medium", "high", "unscored"])

    def test_sort_is_risk_rank_then_composite_ascending(self):
        def row(mpn, risk, score):
            return {"nsn": "1", "candidate_mpn": mpn, "risk_level": risk, "pass_count": 0, "fail_count": 0,
                    "marginal_count": 0, "unknown_count": 0, "price_delta_indicative": "9.00",
                    "composite_score": score}
        sc = pd.DataFrame([
            row("high_lowscore", "high", "10"),
            row("med_null", "medium", ""),
            row("med_40", "medium", "40"),
            row("med_20", "medium", "20"),
            row("low_30", "low", "30"),
            row("unscored_a", "unscored", ""),
        ])
        order = list(rank_candidates(sc).sort_values("rank_within_nsn")["candidate_mpn"])
        # low before medium before high before unscored; within band, lower composite first; nulls last
        self.assertEqual(order, ["low_30", "med_20", "med_40", "med_null", "high_lowscore", "unscored_a"])


class TestHardGates(unittest.TestCase):
    """CURSOR-composite-risk Phase 0/1: missing is unverified, only a real violation fails."""

    def test_missing_candidate_is_unverified_not_fail(self):
        r = req("voltage_rating", "gte", 250.0, uom="V")
        self.assertEqual(evaluate_hard_gate("gte", r, None), "unverified")
        self.assertEqual(evaluate_hard_gate("gte", r, cand(txt="", status="empty")), "unverified")
        self.assertEqual(evaluate_hard_gate("gte", r, cand(txt="?", status="unparsed")), "unverified")

    def test_gte_fails_only_on_actual_shortfall(self):
        r = req("voltage_rating", "gte", 250.0)
        self.assertEqual(evaluate_hard_gate("gte", r, cand(250.0)), "pass")
        self.assertEqual(evaluate_hard_gate("gte", r, cand(300.0)), "pass")
        self.assertEqual(evaluate_hard_gate("gte", r, cand(249.0)), "fail")  # no 10% mercy band

    def test_temp_range_must_contain_required(self):
        rmin = req("operating_temp_min", "lte", -55.0)
        rmax = req("operating_temp_max", "gte", 125.0)
        self.assertEqual(evaluate_hard_gate("lte", rmin, cand(-65.0)), "pass")
        self.assertEqual(evaluate_hard_gate("lte", rmin, cand(-40.0)), "fail")
        self.assertEqual(evaluate_hard_gate("gte", rmax, cand(155.0)), "pass")
        self.assertEqual(evaluate_hard_gate("gte", rmax, cand(85.0)), "fail")
        self.assertEqual(evaluate_hard_gate("gte", rmax, cand(lo=-55.0, hi=155.0, status="range")), "pass")

    def test_exact_num_and_text(self):
        self.assertEqual(evaluate_hard_gate("exact_num", req("contact_count", "eq", 37.0), cand(37.0)), "pass")
        self.assertEqual(evaluate_hard_gate("exact_num", req("contact_count", "eq", 37.0), cand(36.0)), "fail")
        self.assertEqual(evaluate_hard_gate("exact_text", req("coupling_type", "eq", txt="BAYONET", status="text"),
                                           cand(txt="BAYONETLOCK", status="text")), "pass")
        self.assertEqual(evaluate_hard_gate("exact_text", req("coupling_type", "eq", txt="BAYONET", status="text"),
                                           cand(txt="THREADED", status="text")), "fail")

    def test_tolerance_band(self):
        r = req("resistance", "eq", 10_000.0)
        self.assertEqual(evaluate_hard_gate("band", r, cand(10_050.0), band_pct=1.0), "pass")
        self.assertEqual(evaluate_hard_gate("band", r, cand(10_200.0), band_pct=1.0), "fail")

    def test_sealing_meets_or_exceeds(self):
        herm = req("sealing", "eq", txt="HERMETIC", status="text")
        panel = req("sealing", "eq", txt="PANELSEALED", status="text")
        self.assertEqual(evaluate_hard_gate("sealing", herm, cand(txt="HERMETIC", status="text")), "pass")
        self.assertEqual(evaluate_hard_gate("sealing", herm, cand(txt="PANELSEALED", status="text")), "fail")
        self.assertEqual(evaluate_hard_gate("sealing", panel, cand(txt="HERMETIC", status="text")), "pass")

    def test_5945_does_not_gate_voltage_rating(self):
        attrs = hard_gate_attrs("5945")
        self.assertIn("coil_voltage", attrs)
        self.assertNotIn("voltage_rating", attrs)
        self.assertIn("voltage_rating", hard_gate_attrs("5935"))

    def test_missing_does_not_eliminate(self):
        # 5935: contact_count matches, coupling_type absent → survivor
        reqs = {
            "contact_count": req("contact_count", "eq", 37.0),
            "shell_size": req("shell_size", "eq", 23.0),
            "coupling_type": req("coupling_type", "eq", txt="BAYONET", status="text"),
            "operating_temp_min": req("operating_temp_min", "lte", -55.0),
            "operating_temp_max": req("operating_temp_max", "gte", 125.0),
        }
        cands = {"contact_count": cand(37.0), "shell_size": cand(23.0), "operating_temp_min": cand(-65.0)}
        self.assertEqual(hard_gates_failed("5935", reqs, cands), [])
        cands["contact_count"] = cand(2.0)
        self.assertEqual(hard_gates_failed("5935", reqs, cands), ["contact_count"])


class TestCompositeFormulas(unittest.TestCase):
    def test_type_a_lower_is_better(self):
        # original 100, worst 120; 110 → 50; below original → 0; above worst → 100
        self.assertAlmostEqual(attr_soft_risk("dissipation_factor", 110.0, 100.0), 50.0)
        self.assertAlmostEqual(attr_soft_risk("insertion_loss", 90.0, 100.0), 0.0)
        self.assertAlmostEqual(attr_soft_risk("contact_resistance", 130.0, 100.0), 100.0)
        self.assertAlmostEqual(attr_soft_risk("temperature_coefficient", 100.0, 100.0), 0.0)
        self.assertAlmostEqual(attr_soft_risk("dc_resistance", 110.0, 100.0), 50.0)
        self.assertAlmostEqual(attr_soft_risk("coil_resistance", 110.0, 100.0), 50.0)

    def test_type_b_higher_is_better(self):
        self.assertAlmostEqual(attr_soft_risk("mechanical_life", 90.0, 100.0), 50.0)
        self.assertAlmostEqual(attr_soft_risk("q_factor", 110.0, 100.0), 0.0)
        self.assertAlmostEqual(attr_soft_risk("insulation_resistance", 80.0, 100.0), 100.0)
        # headroom above a hard-gate floor: 2× the required current → 0 risk
        self.assertAlmostEqual(attr_soft_risk("current_rating", 2.0, 1.0), 0.0)
        self.assertAlmostEqual(attr_soft_risk("voltage_rating", 0.8, 1.0), 100.0)

    def test_type_c_closer_is_better(self):
        self.assertAlmostEqual(attr_soft_risk("actuation_pressure", 1.1, 1.0), 50.0)
        self.assertAlmostEqual(attr_soft_risk("resistance_tolerance", 0.1, 0.5), 100.0)
        self.assertAlmostEqual(attr_soft_risk("capacitance_tolerance", 0.5, 0.5), 0.0)
        self.assertAlmostEqual(attr_soft_risk("inductance_tolerance", 1.0, 1.0), 0.0)

    def test_type_d_categorical_containment(self):
        self.assertEqual(attr_soft_risk("contact_plating", "gold over nickel", None), 0.0)
        self.assertEqual(attr_soft_risk("contact_plating", "silver", None), 20.0)
        self.assertEqual(attr_soft_risk("contact_plating", "tin", None), 45.0)
        self.assertEqual(attr_soft_risk("contact_plating", "nickel", None), 80.0)
        self.assertEqual(attr_soft_risk("shell_material", "aluminum alloy", None), 30.0)
        self.assertEqual(attr_soft_risk("shell_material", "stainless steel", None), 20.0)
        self.assertEqual(attr_soft_risk("shell_material", "composite", None), 10.0)
        self.assertEqual(attr_soft_risk("dielectric_type", "C0G/NP0", None), 0.0)
        self.assertEqual(attr_soft_risk("dielectric_type", "X7R", None), 30.0)
        self.assertEqual(attr_soft_risk("dielectric_type", "Z5U", None), 70.0)
        self.assertEqual(attr_soft_risk("dielectric_type", "ceramic", None), 85.0)

    def test_universal_lead_time_and_unit_cost(self):
        self.assertAlmostEqual(attr_soft_risk("lead_time", 90.0, None), 50.0)
        self.assertAlmostEqual(attr_soft_risk("lead_time", 180.0, None), 100.0)
        self.assertAlmostEqual(attr_soft_risk("lead_time", 0.0, None), 0.0)
        self.assertAlmostEqual(attr_soft_risk("unit_cost", 10.0, 10.0), 50.0)
        self.assertAlmostEqual(attr_soft_risk("unit_cost", 15.0, 10.0), 100.0)
        self.assertAlmostEqual(attr_soft_risk("unit_cost", 5.0, 10.0), 0.0)

    def test_original_zero_or_missing_is_unavailable(self):
        self.assertIsNone(attr_soft_risk("dissipation_factor", 1.0, 0.0))
        self.assertIsNone(attr_soft_risk("dissipation_factor", 1.0, None))
        self.assertIsNone(attr_soft_risk("dissipation_factor", None, 1.0))
        self.assertIsNone(attr_soft_risk("mechanical_life", 1.0, 0.0))
        self.assertIsNone(attr_soft_risk("actuation_pressure", 1.0, 0.0))
        self.assertIsNone(attr_soft_risk("contact_plating", None, None))
        self.assertIsNone(attr_soft_risk("lead_time", None, None))
        self.assertIsNone(attr_soft_risk("unit_cost", 10.0, None))
        self.assertIsNone(attr_soft_risk("unit_cost", None, 10.0))
        self.assertIsNone(attr_soft_risk("unit_cost", 10.0, 0.0))
        self.assertIsNone(attr_soft_risk("temperature_coefficient", "-100/ +600ppm/°C", 100.0))


class TestRenormalization(unittest.TestCase):
    def test_unenriched_5935_uses_category_pool_only(self):
        # no lead_time / unit_cost → drop the 60-point universal pool
        c = composite("5935",
                      {"contact_resistance": 0.0014, "insulation_resistance": 5e9,
                       "contact_plating": "gold", "shell_material": "composite"},
                      {"contact_resistance": 0.0014, "insulation_resistance": 5e9})
        # Type D: gold=0, composite=10. (13+10+10+7)=40; 7*10/40 = 1.75
        self.assertAlmostEqual(c.score, 1.75)
        self.assertAlmostEqual(c.weight_coverage_pct, 40.0)
        self.assertAlmostEqual(c.available_weight, 40.0)

    def test_partial_category_renormalizes(self):
        # only contact_plating (weight 10) available
        c = composite("5935", {"contact_plating": "gold"}, {})
        self.assertAlmostEqual(c.score, 0.0)
        self.assertAlmostEqual(c.weight_coverage_pct, 10.0)

    def test_5961_unenriched_is_null_not_zero(self):
        c = composite("5961", {}, {})
        self.assertIsNone(c.score)
        self.assertEqual(c.weight_coverage_pct, 0.0)
        self.assertEqual(c.available_weight, 0.0)
        self.assertEqual(risk_from_composite(c.score, []), "unscored")

    def test_5915_and_5961_headroom_weights(self):
        self.assertEqual(CATEGORY_WEIGHTS["5915"], {"insertion_loss": 20, "current_rating": 20})
        self.assertEqual(CATEGORY_WEIGHTS["5961"], {"voltage_rating": 20, "current_rating": 20})
        self.assertEqual(sum(CATEGORY_WEIGHTS["5915"].values()), 40)
        self.assertEqual(sum(CATEGORY_WEIGHTS["5961"].values()), 40)
        # 2× required current, insertion_loss at original → category score 0, coverage 40
        c = composite("5915", {"insertion_loss": 1.0, "current_rating": 2.0},
                      {"insertion_loss": 1.0, "current_rating": 1.0})
        self.assertAlmostEqual(c.score, 0.0)
        self.assertAlmostEqual(c.weight_coverage_pct, 40.0)
        c = composite("5961", {"voltage_rating": 50.0, "current_rating": 2.0},
                      {"voltage_rating": 25.0, "current_rating": 1.0})
        self.assertAlmostEqual(c.score, 0.0)
        self.assertAlmostEqual(c.weight_coverage_pct, 40.0)

    def test_universal_weights_sum_60_and_category_40(self):
        self.assertEqual(UNIVERSAL_WEIGHTS["lead_time"], 38)
        self.assertEqual(UNIVERSAL_WEIGHTS["unit_cost"], 22)
        self.assertEqual(sum(UNIVERSAL_WEIGHTS.values()), 60)
        self.assertEqual(sum(CATEGORY_WEIGHTS["5935"].values()), 40)

    def test_lead_time_raises_coverage_from_40_to_78(self):
        c = composite("5935",
                      {"contact_resistance": 0.0014, "insulation_resistance": 5e9,
                       "contact_plating": "gold", "shell_material": "composite"},
                      {"contact_resistance": 0.0014, "insulation_resistance": 5e9},
                      lead_time_days=90.0)
        self.assertAlmostEqual(c.weight_coverage_pct, 78.0)
        # lead_time 50 (w=38) + composite shell 10 (w=7)
        self.assertAlmostEqual(c.score, (38 * 50.0 + 7 * 10.0) / 78.0)


class TestRiskBandsAndGates(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(risk_from_composite(0.0, []), "low")
        self.assertEqual(risk_from_composite(33.0, []), "low")
        self.assertEqual(risk_from_composite(34.0, []), "medium")
        self.assertEqual(risk_from_composite(66.0, []), "medium")
        self.assertEqual(risk_from_composite(67.0, []), "high")
        self.assertEqual(risk_from_composite(100.0, []), "high")
        self.assertEqual(risk_from_composite(None, []), "unscored")

    def test_any_hard_gate_fail_is_high_100(self):
        self.assertEqual(risk_from_composite(0.0, ["contact_count"]), "high")
        # caller emits composite_score=100 when gates fail; this function only sets the band
        self.assertEqual(risk_from_composite(None, ["operating_temp_min"]), "high")

    def test_gate_type_column(self):
        self.assertEqual(gate_type("5935", "contact_count", "electrical"), "hard")
        self.assertEqual(gate_type("5935", "coupling_type", "mechanical"), "hard")
        self.assertEqual(gate_type("5935", "contact_plating", "electrical"), "soft")
        self.assertEqual(gate_type("5935", "qualification", "qualification"), "burden")
        self.assertEqual(gate_type("5945", "voltage_rating", "electrical"), "soft")
        self.assertEqual(gate_type("5945", "coil_voltage", "electrical"), "hard")
        self.assertEqual(gate_type("5905", "operating_temp_min", "environmental"), "hard")


if __name__ == "__main__":
    unittest.main()
