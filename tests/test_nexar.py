"""Auth helpers for S7. Run: .venv/bin/python -m unittest tests.test_nexar -v"""
import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from s07_nexar_enrich import Nexar  # noqa: E402


class TestNexarBearer(unittest.TestCase):
    def test_supplied_access_token_skips_identity(self):
        posts: list = []

        class _HTTP:
            def post(self, *args, **kwargs):
                posts.append((args, kwargs))
                raise AssertionError("identity server must not be called")

        session = SimpleNamespace(session=_HTTP())
        nx = Nexar(logging.getLogger("test"), session, pro_fields=False, dry_run=True)
        nx._token = "supplied-jwt"
        nx._token_source = "access_token"
        self.assertEqual(nx.token(), "supplied-jwt")
        self.assertEqual(posts, [])
