"""Smoke tests for S8 normalize. Run: .venv/bin/python -m unittest discover -s tests -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import logging

import pandas as pd

from common import Aliases, DroppedRows  # noqa: E402
from s08_normalize import (  # noqa: E402
    normalize_value, normalize_package, normalize_lifecycle, split_range_attr, infer_operator, bare_value,
    canonical_operator, normalize_set, normalize_boolean, OPERATORS, LIFECYCLES, value_with_uom,
    normalize_profiles, normalize_candidates, normalize_external_candidates, gather_normalized_candidates,
    contextual_attr, CANDIDATE_SOURCES,
)


class TestSeparateUomColumn(unittest.TestCase):
    """PUB LOG / spec rows carry the unit in a separate `uom` column with a bare number in `value`.
    Ignoring it puts the number in the base unit — 10000 pF read as 10000 F, 1000 Mohm as 1000 ohm —
    which then scores every candidate confidently wrong. This class guards that path end to end."""

    def test_value_with_uom_only_when_value_is_bare(self):
        self.assertEqual(value_with_uom("10000", "pF"), "10000 pF")
        self.assertEqual(value_with_uom("1000", "Mohm"), "1000 Mohm")
        self.assertEqual(value_with_uom("-55", "C"), "-55 C")
        self.assertEqual(value_with_uom("10000 pF", "pF"), "10000 pF")     # value already has its unit
        self.assertEqual(value_with_uom("composite", "text"), "composite")  # non-unit uom left alone
        self.assertEqual(value_with_uom("25", "count"), "25")               # unknown unit -> no change
        self.assertEqual(value_with_uom("10", "cycles"), "10")
        self.assertEqual(value_with_uom("5", ""), "5")

    def _profiles(self, rows):
        df = pd.DataFrame(rows)
        for c in ("operator", "source", "requirement_class", "source_spec_id", "conflicting_value", "conflicting_source"):
            if c not in df.columns:
                df[c] = ""
        df = df.fillna("")
        return normalize_profiles(df, Aliases(), DroppedRows(logging.getLogger("t"), "t"))

    def test_profiles_carry_uom_into_base_units(self):
        out = self._profiles([
            {"nsn": "5910-00-000-0001", "requirement_name": "capacitance", "value": "10000.0", "uom": "pF"},
            {"nsn": "5935-00-000-0001", "requirement_name": "insulation_resistance", "value": "1000", "uom": "Mohm"},
            {"nsn": "5935-00-000-0001", "requirement_name": "operating_temp_min", "value": "-55", "uom": "C", "operator": "lte"},
            {"nsn": "5935-00-000-0001", "requirement_name": "operating_temp_max", "value": "125", "uom": "C", "operator": "gte"},
            {"nsn": "5935-00-000-0001", "requirement_name": "contact_count", "value": "25", "uom": "count", "operator": "eq"},
            {"nsn": "5905-00-000-0001", "requirement_name": "temperature_coefficient", "value": "50", "uom": "ppm_per_C"},
        ])
        by = {r["requirement_name"]: r for _, r in out.iterrows()}
        self.assertAlmostEqual(float(by["capacitance"]["value_num"]), 1e-8, places=15)
        self.assertEqual(by["capacitance"]["uom"], "F")
        self.assertAlmostEqual(float(by["insulation_resistance"]["value_num"]), 1e9)
        self.assertEqual(by["insulation_resistance"]["uom"], "ohm")
        self.assertEqual(float(by["operating_temp_min"]["value_num"]), -55.0)
        self.assertEqual(float(by["operating_temp_max"]["value_num"]), 125.0)
        self.assertEqual(by["operating_temp_max"]["uom"], "degC")
        self.assertEqual(float(by["contact_count"]["value_num"]), 25.0)   # unitless count still a number
        self.assertEqual(float(by["temperature_coefficient"]["value_num"]), 50.0)
        self.assertEqual(by["temperature_coefficient"]["uom"], "ppm/degC")
        self.assertTrue((out["parse_status"] == "number").all(), out[["requirement_name", "parse_status"]])

    def test_digikey_value_shapes(self):
        # test conditions after "@" are dropped; the value before it is the rating
        n = normalize_value("30 @ 7.9MHz", "q_factor")
        self.assertEqual((n.kind, n.value_num), ("number", 30.0))
        n = normalize_value("1.1V @ 1A", "voltage_rating")
        self.assertEqual((n.kind, n.value_num, n.uom), ("number", 1.1, "V"))
        # ceramic dielectric class filed under "Temperature Coefficient" is a dielectric, not a tempco
        self.assertEqual(contextual_attr("temperature_coefficient", {"capacitance"}, "X7R"), "dielectric_type")
        self.assertEqual(contextual_attr("temperature_coefficient", {"capacitance"}, "C0G, NP0"), "dielectric_type")
        self.assertEqual(contextual_attr("temperature_coefficient", {"resistance"}, "±100ppm/°C"), "temperature_coefficient")
        self.assertEqual(normalize_value("C0G, NP0", "dielectric_type").value_text, "C0G")
        self.assertEqual(normalize_value("CERAMIC COG", "dielectric_type").value_text, "C0G")
        self.assertEqual(normalize_value("X7R", "dielectric_type").value_text, "X7R")
        self.assertEqual(normalize_value("Metallized Paper", "dielectric_type").value_text, "METALLIZEDPAPER")
        # MIL-DTL-38999 shell size letters -> numbers
        self.assertEqual(normalize_value("H", "shell_size").value_num, 23.0)
        self.assertEqual(normalize_value("23", "shell_size").value_num, 23.0)

    def test_candidate_tolerance_is_resolved_by_primary_quantity(self):
        self.assertEqual(contextual_attr("tolerance", {"resistance", "power_rating"}), "resistance_tolerance")
        self.assertEqual(contextual_attr("tolerance", {"capacitance"}), "capacitance_tolerance")
        self.assertEqual(contextual_attr("tolerance", {"inductance"}), "inductance_tolerance")
        self.assertEqual(contextual_attr("tolerance", {"contact_count"}), "tolerance")
        cands = pd.DataFrame([{
            "nsn": "5905-00-000-0001", "candidate_mpn": "X", "manufacturer": "M", "digikey_pn": "D",
            "raw_attributes_json": '{"Resistance": "150 Ohms", "Tolerance": "±1%", "Power (Watts)": "3W", '
                                   '"Operating Temperature": "-55°C ~ 175°C", "_status": "Active"}',
        }])
        out = normalize_candidates(cands, Aliases(), DroppedRows(logging.getLogger("t"), "t"))
        by = {r["attribute_name"]: r for _, r in out.iterrows()}
        self.assertIn("resistance_tolerance", by)
        self.assertNotIn("tolerance", by)
        self.assertEqual(float(by["resistance_tolerance"]["value_num"]), 1.0)
        self.assertEqual(float(by["operating_temp_max"]["value_num"]), 175.0)
        self.assertEqual(float(by["operating_temp_min"]["value_num"]), -55.0)
        self.assertNotIn("_status", [r["raw_name"] for _, r in out.iterrows()])


class TestNumbers(unittest.TestCase):
    def assertNum(self, raw, attr, expected, uom, places=9):
        n = normalize_value(raw, attr)
        self.assertEqual(n.kind, "number", f"{raw!r} -> {n}")
        self.assertAlmostEqual(n.value_num, expected, places=places, msg=f"{raw!r} -> {n}")
        self.assertEqual(n.uom, uom, f"{raw!r} -> {n}")

    def test_publog_words(self):
        self.assertNum("10.0 KILOHMS", "resistance", 10_000.0, "ohm")
        self.assertNum("4.7 MEGOHMS", "resistance", 4.7e6, "ohm")
        self.assertNum("0.125 WATTS", "power_rating", 0.125, "W")
        self.assertNum("50.0 VOLTS", "voltage_rating", 50.0, "V")
        self.assertNum("1.0 PERCENT", "tolerance", 1.0, "%")
        self.assertNum("100.0 PICOFARADS", "capacitance", 100e-12, "F", places=15)
        self.assertNum("2.0 AMPERES", "current_rating", 2.0, "A")

    def test_digikey_symbols(self):
        self.assertNum("10 kOhms", "resistance", 10_000.0, "ohm")
        self.assertNum("1 MOhms", "resistance", 1e6, "ohm")
        self.assertNum("50 mOhms", "resistance", 0.05, "ohm")
        self.assertNum("0.1 µF", "capacitance", 1e-7, "F", places=12)
        self.assertNum("22pF", "capacitance", 22e-12, "F", places=15)
        self.assertNum("50V", "voltage_rating", 50.0, "V")
        self.assertNum("±5%", "tolerance", 5.0, "%")
        self.assertNum("±1%", "tolerance", 1.0, "%")
        self.assertNum("±100ppm/°C", "temperature_coefficient", 100.0, "ppm/degC")
        self.assertNum("125mW", "power_rating", 0.125, "W")

    def test_fractions_and_compounds(self):
        self.assertNum("1/8 W", "power_rating", 0.125, "W")
        self.assertNum("0.125W, 1/8W", "power_rating", 0.125, "W")
        self.assertNum("1/4W", "power_rating", 0.25, "W")

    def test_units_without_prefix_are_kept(self):
        self.assertNum("125", "operating_temp_max", 125.0, "degC")

    def test_unknown_unit_is_unparsed_not_zero(self):
        n = normalize_value("10 furlongs", "resistance")
        self.assertEqual(n.kind, "unparsed")
        self.assertIsNone(n.value_num)

    def test_wrong_family_is_unparsed(self):
        n = normalize_value("10 V", "resistance")
        self.assertEqual(n.kind, "unparsed")

    def test_empty(self):
        self.assertEqual(normalize_value("", "resistance").kind, "empty")
        self.assertEqual(normalize_value("N/A", "resistance").kind, "empty")


class TestRanges(unittest.TestCase):
    def assertRange(self, raw, attr, lo, hi, uom):
        n = normalize_value(raw, attr)
        self.assertEqual(n.kind, "range", f"{raw!r} -> {n}")
        self.assertAlmostEqual(n.value_min, lo, msg=f"{raw!r} -> {n}")
        self.assertAlmostEqual(n.value_max, hi, msg=f"{raw!r} -> {n}")
        self.assertEqual(n.uom, uom)

    def test_digikey_tilde(self):
        self.assertRange("-55°C ~ 125°C", "operating_temp", -55.0, 125.0, "degC")
        self.assertRange("-55°C ~ 155°C (TA)", "operating_temp", -55.0, 155.0, "degC")

    def test_publog_slash(self):
        self.assertRange("-55.0/+125.0 DEG CELSIUS", "operating_temp", -55.0, 125.0, "degC")

    def test_words(self):
        self.assertRange("-55 to +125 C", "operating_temp", -55.0, 125.0, "degC")
        self.assertRange("-65.0 DEG CELSIUS AND +150.0 DEG CELSIUS", "operating_temp", -65.0, 150.0, "degC")

    def test_split_into_min_max(self):
        n = normalize_value("-55°C ~ 125°C", "operating_temp")
        parts = split_range_attr("operating_temp", n)
        names = {p[0]: (p[1].value_num, p[2]) for p in parts}
        self.assertEqual(names["operating_temp_min"], (-55.0, "lte"))
        self.assertEqual(names["operating_temp_max"], (125.0, "gte"))

    def test_range_contract_value_is_min_pipe_max(self):
        # SCHEMA §8: 'range' uses value formatted "min|max" — and the contract form must re-parse (idempotent)
        n = normalize_value("-55 to +125 C", "operating_temp")
        self.assertEqual(bare_value(n.as_row()), "-55|125")
        self.assertRange("-55|125", "operating_temp", -55.0, 125.0, "degC")
        self.assertRange("-55|125 degC", "operating_temp", -55.0, 125.0, "degC")

    def test_single_temp_becomes_max(self):
        n = normalize_value("125 DEG CELSIUS", "operating_temp")
        parts = split_range_attr("operating_temp", n)
        self.assertEqual([p[0] for p in parts], ["operating_temp_max"])

    def test_fraction_is_not_a_range(self):
        n = normalize_value("1/8 W", "power_rating")
        self.assertEqual(n.kind, "number")


class TestText(unittest.TestCase):
    def test_package(self):
        self.assertEqual(normalize_package("0603 (1608 Metric)"), "0603")
        self.assertEqual(normalize_package("Axial"), "AXIAL")
        self.assertEqual(normalize_value("Radial, Can", "package_case").value_text, "RADIALCAN")

    def test_text_attr_kind(self):
        n = normalize_value("Thick Film", "composition")
        self.assertEqual((n.kind, n.value_text), ("text", "THICKFILM"))


class TestOperators(unittest.TestCase):
    def test_closed_enum(self):
        self.assertEqual(OPERATORS, {"eq", "gte", "lte", "range", "in_set", "boolean"})

    def test_defaults_and_aliases(self):
        num = normalize_value("0.125 W", "power_rating")
        self.assertEqual(infer_operator("power_rating", "", num), "gte")
        self.assertEqual(infer_operator("tolerance", "", num), "lte")
        self.assertEqual(infer_operator("resistance", "", num), "eq")
        self.assertEqual(infer_operator("resistance", ">=", num), "gte")   # legacy symbol mapped
        self.assertEqual(infer_operator("resistance", "=", num), "eq")
        self.assertEqual(infer_operator("resistance", "GTE", num), "gte")
        rng = normalize_value("-55 to 125 C", "operating_temp")
        self.assertEqual(infer_operator("operating_temp", "", rng), "range")
        self.assertEqual(infer_operator("operating_temp", "eq", rng), "range")  # value shape wins
        self.assertIsNone(canonical_operator("approximately"))
        for given in ("", ">=", "<=", "==", "range", "in", "bool"):
            self.assertIn(infer_operator("resistance", given, num), OPERATORS)

    def test_in_set_and_boolean_values(self):
        n = normalize_set("0603 (1608 Metric)|0805", "package_case")
        self.assertEqual((n.kind, bare_value(n.as_row())), ("set", "0603|0805"))
        n = normalize_set("10 kOhms|22 kOhms", "resistance")
        self.assertEqual((n.kind, bare_value(n.as_row()), n.uom), ("set", "10000|22000", "ohm"))
        self.assertEqual(normalize_set("10 kOhms|banana", "resistance").kind, "unparsed")
        self.assertEqual((normalize_boolean("Yes").kind, normalize_boolean("Yes").value_text), ("boolean", "true"))
        self.assertEqual(normalize_boolean("not required").value_text, "false")
        self.assertEqual(normalize_boolean("maybe").kind, "unparsed")


class TestLifecycle(unittest.TestCase):
    def test_closed_enum(self):
        # SCHEMA §8: active | nrnd | obsolete | unknown — nothing else leaves the pipeline
        for raw in ("Active", "Production", "Not Recommended for New Designs", "NRND", "Obsolete", "Discontinued",
                    "Last Time Buy", "End of Life", "EOL", "", "something odd"):
            self.assertIn(normalize_lifecycle(raw), LIFECYCLES, raw)

    def test_map(self):
        self.assertEqual(normalize_lifecycle("Active"), "active")
        self.assertEqual(normalize_lifecycle("Not Recommended for New Designs"), "nrnd")
        self.assertEqual(normalize_lifecycle("Obsolete"), "obsolete")
        self.assertEqual(normalize_lifecycle("Last Time Buy"), "obsolete")   # LTB/EOL -> obsolete for substitution
        self.assertEqual(normalize_lifecycle(""), "unknown")
        self.assertEqual(normalize_lifecycle("something odd"), "unknown")


class TestExternalCandidates(unittest.TestCase):
    """data/candidates_external.csv is long-format (one attribute per row), same vocabulary as
    the spec. S8 must normalise it next to Digi-Key candidates and tag `source` so S9 can split."""

    def _dropped(self):
        return DroppedRows(logging.getLogger("t"), "t")

    def _ext(self, rows):
        df = pd.DataFrame(rows)
        for c in ("manufacturer", "uom"):
            if c not in df.columns:
                df[c] = ""
        return normalize_external_candidates(df.fillna(""), Aliases(), self._dropped())

    def test_source_tag_is_external_and_units_come_from_the_uom_column(self):
        out = self._ext([
            {"nsn": "5935-00-104-9650", "candidate_mpn": "TXR40AB00-1210BI", "manufacturer": "TE",
             "attribute_name": "operating_temp_max", "attribute_value": "175", "uom": "C"},
            {"nsn": "5935-00-104-9650", "candidate_mpn": "TXR40AB00-1210BI", "manufacturer": "TE",
             "attribute_name": "operating_temp_min", "attribute_value": "-65", "uom": "C"},
            {"nsn": "5935-00-104-9650", "candidate_mpn": "TXR40AB00-1210BI", "manufacturer": "TE",
             "attribute_name": "shell_material", "attribute_value": "aluminum", "uom": ""},
        ])
        by = {r["attribute_name"]: r for _, r in out.iterrows()}
        self.assertEqual(set(out["source"]), {"external"})
        self.assertEqual(float(by["operating_temp_max"]["value_num"]), 175.0)
        self.assertEqual(by["operating_temp_max"]["uom"], "degC")
        self.assertEqual(float(by["operating_temp_min"]["value_num"]), -65.0)
        self.assertEqual(by["shell_material"]["value_text"], "ALUMINUM")
        self.assertEqual(by["operating_temp_max"]["digikey_pn"], "")

    def test_same_mpn_on_two_nsns_is_two_candidates(self):
        # one commercial part can be a candidate for several stock numbers
        out = self._ext([
            {"nsn": "5999-01-129-4193", "candidate_mpn": "2118707-2", "attribute_name": "operating_temp_max",
             "attribute_value": "85", "uom": "C"},
            {"nsn": "5999-01-222-0064", "candidate_mpn": "2118707-2", "attribute_name": "operating_temp_max",
             "attribute_value": "85", "uom": "C"},
        ])
        pairs = set(zip(out["nsn"], out["candidate_mpn"]))
        self.assertEqual(pairs, {("5999-01-129-4193", "2118707-2"), ("5999-01-222-0064", "2118707-2")})

    def test_duplicate_triples_keep_first(self):
        out = self._ext([
            {"nsn": "N", "candidate_mpn": "M", "attribute_name": "current_rating", "attribute_value": "1", "uom": "A"},
            {"nsn": "N", "candidate_mpn": "M", "attribute_name": "current_rating", "attribute_value": "2", "uom": "A"},
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(float(out.iloc[0]["value_num"]), 1.0)

    def test_gather_concatenates_digikey_and_external_without_colliding(self):
        digikey = pd.DataFrame([{
            "nsn": "5905-00-000-0001", "candidate_mpn": "CRCW", "manufacturer": "Vishay", "digikey_pn": "D1",
            "raw_attributes_json": '{"Resistance": "150 Ohms"}',
        }])
        external = pd.DataFrame([{
            "nsn": "5935-00-104-9650", "candidate_mpn": "TXR40", "manufacturer": "TE",
            "attribute_name": "operating_temp_max", "attribute_value": "175", "uom": "C",
        }])
        out = gather_normalized_candidates(Aliases(), self._dropped(), digikey=digikey, external=external)
        self.assertEqual(CANDIDATE_SOURCES, ("digikey", "external"))
        self.assertEqual(set(out["source"]), {"digikey", "external"})
        self.assertEqual(out[out["source"] == "digikey"]["candidate_mpn"].tolist(), ["CRCW"])
        self.assertEqual(out[out["source"] == "external"]["candidate_mpn"].tolist(), ["TXR40"])


if __name__ == "__main__":
    unittest.main()
