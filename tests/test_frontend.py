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
        self.assertNotIn("<dialog", HTML)

    def test_removed_controls_stay_gone(self):
        for text in ("pauseBtn", "Pause 10 min", "Reset agents", "stage-controls", "ankiEndpointInput", "Custom sync server", "/api/pause"):
            self.assertNotIn(text, HTML)

    def test_clear_stuck_runs_lives_in_settings(self):
        settings = HTML[HTML.index('id="panel-settings"'):HTML.index("</main>")]
        self.assertIn('id="clearAgentsBtn"', settings)
        self.assertIn("Clear stuck runs", settings)

    def test_sign_in_is_two_steps(self):
        script = inline_script()
        self.assertIn('postJSON("/api/anki/login", body)', script)
        self.assertIn("const FAST_SYNC = { media: false, background_media: true };", script)
        self.assertIn('{ ...FAST_SYNC, on_conflict: "download" }', script)
        self.assertIsNone(re.search(r"[{,]\s*media: true", script))
        for text in ("Connect AnkiWeb", "Downloading your decks…", "Cards appear first; images finish in the background.",
                     "Resync connection", "Signing in…", "never stored. Only a local 0600 sync token is saved."):
            self.assertIn(text, HTML)

    def test_rest_has_a_breath_bar_and_no_numbers(self):
        rest = HTML[HTML.index('id="panel-rest"'):HTML.index('id="panel-settings"')]
        self.assertIn('id="breathBarFill"', rest)
        self.assertEqual(rest.count('class="petal"'), 6)


@unittest.skipUnless(NODE, "node is not installed")
class ScriptTest(unittest.TestCase):
    def test_breath_bar_never_jumps(self):
        block = HTML[HTML.index("    const BAR_RANGE"):HTML.index("    function renderRestNote()")]
        cadences = HTML[HTML.index("    const CADENCES = {"):HTML.index("    const savedCadence")]
        script = cadences + block.replace("$(\"breathBarFill\")", "bar") + """
const bar = { style: {} };
const out = {};
for (const [key, phases] of Object.entries(CADENCES)) {
  const frames = [];
  phases.forEach((p, i) => {
    for (const f of [0, 0.5, 1]) {
      breathBarFrame(phases, i, f, 0.5 - 0.5 * Math.cos(Math.PI * f));
      frames.push([p.dir, f, Number(bar.style.transform.match(/[\\d.]+/)[0]), Number(bar.style.opacity)]);
    }
  });
  out[key] = frames;
}
console.log(JSON.stringify(out));
"""
        out = json.loads(subprocess.run([NODE, "-e", script], check=True, capture_output=True, text=True).stdout)
        for key, frames in out.items():
            with self.subTest(cadence=key):
                # Visible value at each phase end equals the visible value at the next phase start.
                ends = [f for f in frames if f[1] == 1]
                starts = [f for f in frames if f[1] == 0]
                for end, start in zip(ends, starts[1:] + starts[:1]):
                    self.assertAlmostEqual(end[2] * end[3], start[2] * start[3], places=3)
                holds = [f for f in frames if f[0].startswith("hold") and f[1] == 0.5]
                for f in holds:
                    self.assertAlmostEqual(f[2], 0.5, places=3)  # holds glide at a steady pace


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
