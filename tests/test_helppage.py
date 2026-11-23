import os
import tempfile
import unittest

from swarm import helppage

HEADINGS = (
    "What this is",
    "Columns",
    "Plan states",
    "Asks",
    "Header strip",
    "Worktrees",
    "Commands",
)


class HelpPageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("SWARM_HOME")
        os.environ["SWARM_HOME"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        if self._old_home is None:
            os.environ.pop("SWARM_HOME", None)
        else:
            os.environ["SWARM_HOME"] = self._old_home

    def test_headings(self):
        page = helppage.render()
        for heading in HEADINGS:
            self.assertIn(f"<h2>{heading}</h2>", page)

    def test_sections_and_main(self):
        page = helppage.render()
        self.assertIn('<main class="help">', page)
        self.assertEqual(page.count('<main class="help">'), 1)
        self.assertEqual(page.count("</main>"), 1)
        self.assertEqual(page.count("<section>"), len(HEADINGS))

    def test_back_link(self):
        page = helppage.render()
        self.assertIn(
            '<header class="strip"><a class="item" href="dashboard.html">← Board</a></header>',
            page,
        )

    def test_key_terms(self):
        page = helppage.render()
        for text in ("DRAFT", "FIXING", "swarm answer ID", "monthly_budget_usd"):
            self.assertIn(text, page)

    def test_no_refresh_script_or_links(self):
        page = helppage.render()
        for text in ('http-equiv="refresh"', "<script", "http://", "https://"):
            self.assertNotIn(text, page)

    def test_board_name(self):
        page = helppage.render("board.html")
        self.assertIn('href="board.html"', page)
        self.assertNotIn('href="dashboard.html"', page)

    def test_board_name_escaped(self):
        page = helppage.render('a"b.html')
        self.assertIn('href="a&quot;b.html"', page)
