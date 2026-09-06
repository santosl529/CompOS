"""Smoke tests for S6 image extraction. Run: .venv/bin/python -m unittest tests.test_digikey -v"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pandas as pd

from s06_digikey_search import (  # noqa: E402
    parse_product, part_images, first_product_photo, candidate_photo_by_nsn,
)


class TestPartImages(unittest.TestCase):
    def test_parse_product_copies_photourl(self):
        mpn, _mfr, _dk, attrs = parse_product({
            "ManufacturerProductNumber": "CRCW060310K0FKEA",
            "Manufacturer": {"Name": "Vishay Dale"},
            "PhotoUrl": "https://mm.digikey.com/Volume0/opasdata/d220001/medias/images/1/part.jpg",
            "DatasheetUrl": "https://example.com/ds.pdf",
            "ProductVariations": [{"DigiKeyProductNumber": "541-CRCW-ND"}],
        })
        self.assertEqual(mpn, "CRCW060310K0FKEA")
        self.assertEqual(attrs["_photo"], "https://mm.digikey.com/Volume0/opasdata/d220001/medias/images/1/part.jpg")

    def test_part_images_is_unique_mpn_to_url_and_skips_empty(self):
        url = "https://mm.digikey.com/a.jpg"
        rows = [
            {"candidate_mpn": "AA", "raw_attributes_json": json.dumps({"_photo": url})},
            {"candidate_mpn": "AA", "raw_attributes_json": json.dumps({"_photo": "https://other.example/later.jpg"})},
            {"candidate_mpn": "BB", "raw_attributes_json": json.dumps({"_photo": ""})},
            {"candidate_mpn": "CC", "raw_attributes_json": json.dumps({"Resistance": "10"})},
        ]
        out = part_images(rows)
        self.assertEqual(list(out.columns), ["mpn", "image_url"])
        self.assertEqual(out.to_dict("records"), [{"mpn": "AA", "image_url": url}])

    def test_first_product_photo_skips_empty(self):
        url, mpn = first_product_photo([
            {"ManufacturerProductNumber": "X", "PhotoUrl": ""},
            {"ManufacturerProductNumber": "Y", "PhotoUrl": "https://mm.digikey.com/y.jpg"},
        ])
        self.assertEqual((url, mpn), ("https://mm.digikey.com/y.jpg", "Y"))
        self.assertEqual(first_product_photo([]), ("", ""))

    def test_first_product_photo_rejects_unrelated_keyword_hit(self):
        products = [{
            "ManufacturerProductNumber": "LTST-C171KGKT",
            "PhotoUrl": "https://mm.digikey.com/led.jpg",
            "Description": {"ProductDescription": "LED GREEN"},
        }]
        self.assertEqual(first_product_photo(products, query="1203-1"), ("", ""))
        self.assertEqual(first_product_photo(products, query="289A651YL02C750"), ("", ""))
        products[0]["ManufacturerProductNumber"] = "289A651YL02C750"
        url, mpn = first_product_photo(products, query="289A651YL02C750")
        self.assertEqual(mpn, "289A651YL02C750")

    def test_candidate_photo_by_nsn_first_hit_wins(self):
        cands = pd.DataFrame([
            {"nsn": "A", "candidate_mpn": "P1"},
            {"nsn": "A", "candidate_mpn": "P2"},
            {"nsn": "B", "candidate_mpn": "P3"},
        ])
        images = pd.DataFrame([
            {"mpn": "P2", "image_url": "https://mm.digikey.com/p2.jpg"},
            {"mpn": "P3", "image_url": "https://mm.digikey.com/p3.jpg"},
        ])
        out = candidate_photo_by_nsn(cands, images)
        self.assertEqual(out["A"], ("https://mm.digikey.com/p2.jpg", "P2"))
        self.assertEqual(out["B"], ("https://mm.digikey.com/p3.jpg", "P3"))


if __name__ == "__main__":
    unittest.main()
