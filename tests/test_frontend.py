"""Static checks on static/index.html: unique ids, valid inline JS, and the blocked-site classifier."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import support

INDEX = support.ROOT / "static" / "index.html"
HTML = INDEX.read_text(encoding="utf-8")
NODE = shutil.which("node")


def inline_script() -> str:
    return "\n;\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", HTML, re.S))


class MarkupTest(unittest.TestCase):
    def test_ids_are_unique(self):
        dupes = [i for i, n in Counter(re.findall(r'\bid="([^"]+)"', HTML)).items() if n > 1]
        self.assertEqual(dupes, [])

    def test_every_dollar_lookup_has_an_element(self):
        ids = set(re.findall(r'\bid="([^"]+)"', HTML))
        missing = sorted({i for i in re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', HTML)} - ids)
        self.assertEqual(missing, [])

    def test_no_legacy_or_contradictory_copy(self):
        for text in ("AnkiConnect", "Anki Desktop", "Look at something across the room", "—"):
            self.assertNotIn(text, HTML)

    def test_single_anki_login_form(self):
        self.assertEqual(HTML.count("<form id=\"ankiLoginForm\""), 1)
        self.assertEqual(HTML.count('type="password"'), 1)


@unittest.skipUnless(NODE, "node is not installed")
class ScriptTest(unittest.TestCase):
    def test_inline_script_parses(self):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(inline_script())
        self.addCleanup(Path(f.name).unlink)
        subprocess.run([NODE, "--check", f.name], check=True, capture_output=True)

    def test_domain_classifier(self):
        block = re.search(r"// <domain-classifier>[^\n]*\n(.*?)// </domain-classifier>", HTML, re.S).group(1)
        cases = {
            "mail.proton.me": "Mail & Messaging",
            "outlook.live.com": "Mail & Messaging",
            "gmx.net": "Mail & Messaging",
            "app.slack.com": "Mail & Messaging",
            "webmail.example.org": "Mail & Messaging",
            "old.reddit.com": "Social",
            "x.com": "Social",
            "bsky.app": "Social",
            "tagesschau.de": "News",
            "nzz.ch": "News",
            "washingtonpost.com": "News",
            "lokalzeitung.de": "News",
            "n-tv.de": "News",
            "news.ycombinator.com": "Tech & Forums",
            "heise.de": "Tech & Forums",
            "forum.example.com": "Tech & Forums",
            "youtube.com": "Video & Media",
            "twitch.tv": "Video & Media",
            "mytube.example": "Video & Media",
            "postbank.de": "Other",
            "timesheet.io": "Other",
            "example.com": "Other",
        }
        script = block + f"""
const cases = {json.dumps(cases)};
const got = Object.fromEntries(Object.keys(cases).map((d) => [d, classifyDomain(d)]));
const grouped = groupDomains(["zeit.de", "bbc.com", "x.com", "example.com"]);
console.log(JSON.stringify({{ got, grouped }}));
"""
        out = json.loads(subprocess.run([NODE, "-e", script], check=True, capture_output=True, text=True).stdout)
        self.assertEqual(out["got"], cases)
        self.assertEqual(out["grouped"], [["Social", ["x.com"]], ["News", ["bbc.com", "zeit.de"]], ["Other", ["example.com"]]])


if __name__ == "__main__":
    unittest.main()
