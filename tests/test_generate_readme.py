"""Unit tests for the profile README generator.

These tests never touch the network. They import the generator module and
exercise its pure selection, grouping, and rendering logic with mocked
repository data.

Run from the repository root:

    python -m unittest discover -s tests -v
"""

import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "generate-readme.py"

# The script filename contains a hyphen, so load it explicitly.
_spec = importlib.util.spec_from_file_location("generate_readme", MODULE_PATH)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def make_profile():
    return {
        "owner": "kevinpinscoe",
        "name": "Kevin P. Inscoe",
        "summary": "Software engineer, SRE, infrastructure builder, and technical hobbyist",
        "repository_defaults": {"include_archived": False, "include_forks": False},
        "categories": [
            {"name": "Software Development", "topic": "area-software-development",
             "description": "Go and Python.", "subjects": ["Go", "Python"]},
            {"name": "Weather", "topic": "area-weather",
             "description": "Weather tools.", "subjects": ["Decoding"]},
        ],
        "repositories": {
            "include": [],
            "exclude": [],
            "categories": {},
            "overrides": {},
        },
    }


def repo(name, description="", topics=None, archived=False, fork=False, url=None):
    return {
        "name": name,
        "description": description,
        "url": url or f"https://github.com/kevinpinscoe/{name}",
        "topics": topics or [],
        "archived": archived,
        "fork": fork,
    }


class CategoryAssignmentTests(unittest.TestCase):
    def test_topic_from_github(self):
        profile = make_profile()
        repos = [repo("tool", topics=["area-software-development"])]
        grouped = gen.group_by_category(
            gen.select_repositories(repos, profile, warn=lambda m: None), profile
        )
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["category"]["topic"], "area-software-development")
        self.assertEqual(grouped[0]["repos"][0]["name"], "tool")

    def test_topic_from_profile_map(self):
        profile = make_profile()
        profile["repositories"]["categories"] = {"tool": ["area-software-development"]}
        repos = [repo("tool")]  # no github topics
        selected = gen.select_repositories(repos, profile, warn=lambda m: None)
        self.assertEqual(len(selected), 1)

    def test_repo_in_multiple_categories(self):
        profile = make_profile()
        repos = [repo("get-wx", topics=["area-software-development", "area-weather"])]
        grouped = gen.group_by_category(
            gen.select_repositories(repos, profile, warn=lambda m: None), profile
        )
        self.assertEqual(len(grouped), 2)
        names = [g["repos"][0]["name"] for g in grouped]
        self.assertEqual(names, ["get-wx", "get-wx"])


class FilteringTests(unittest.TestCase):
    def test_archived_and_fork_filtered(self):
        profile = make_profile()
        repos = [
            repo("a", topics=["area-weather"], archived=True),
            repo("b", topics=["area-weather"], fork=True),
            repo("c", topics=["area-weather"]),
        ]
        selected = gen.select_repositories(repos, profile, warn=lambda m: None)
        self.assertEqual([r["name"] for r in selected], ["c"])

    def test_explicit_exclude(self):
        profile = make_profile()
        profile["repositories"]["exclude"] = ["c"]
        repos = [repo("c", topics=["area-weather"])]
        selected = gen.select_repositories(repos, profile, warn=lambda m: None)
        self.assertEqual(selected, [])

    def test_explicit_include_bypasses_filters(self):
        profile = make_profile()
        profile["repositories"]["include"] = ["a"]
        repos = [repo("a", topics=["area-weather"], archived=True)]
        selected = gen.select_repositories(repos, profile, warn=lambda m: None)
        self.assertEqual([r["name"] for r in selected], ["a"])

    def test_no_area_topic_omitted(self):
        profile = make_profile()
        warnings = []
        repos = [repo("orphan", topics=["python"])]
        selected = gen.select_repositories(repos, profile, warn=warnings.append)
        self.assertEqual(selected, [])
        self.assertEqual(len(warnings), 1)


class OverrideAndOrderingTests(unittest.TestCase):
    def test_description_override(self):
        profile = make_profile()
        profile["repositories"]["overrides"] = {"tool": {"description": "Override desc."}}
        repos = [repo("tool", description="github desc", topics=["area-software-development"])]
        grouped = gen.group_by_category(
            gen.select_repositories(repos, profile, warn=lambda m: None), profile
        )
        self.assertEqual(grouped[0]["repos"][0]["description"], "Override desc.")

    def test_neutral_fallback_description(self):
        profile = make_profile()
        repos = [repo("tool", description="", topics=["area-software-development"])]
        grouped = gen.group_by_category(
            gen.select_repositories(repos, profile, warn=lambda m: None), profile
        )
        self.assertEqual(grouped[0]["repos"][0]["description"], "No description provided.")

    def test_explicit_order_then_alphabetical(self):
        profile = make_profile()
        profile["repositories"]["overrides"] = {"zeta": {"order": 1}}
        repos = [
            repo("alpha", topics=["area-software-development"]),
            repo("zeta", topics=["area-software-development"]),
            repo("beta", topics=["area-software-development"]),
        ]
        grouped = gen.group_by_category(
            gen.select_repositories(repos, profile, warn=lambda m: None), profile
        )
        names = [r["name"] for r in grouped[0]["repos"]]
        # zeta has explicit order -> first; rest alphabetical
        self.assertEqual(names, ["zeta", "alpha", "beta"])


class RenderingTests(unittest.TestCase):
    def _grouped(self, profile, repos):
        return gen.group_by_category(
            gen.select_repositories(repos, profile, warn=lambda m: None), profile
        )

    def test_markdown_is_deterministic(self):
        profile = make_profile()
        repos = [
            repo("tool", description="A tool", topics=["area-software-development"]),
            repo("wx", description="Weather", topics=["area-weather"]),
        ]
        grouped = self._grouped(profile, repos)
        out1 = gen.render_markdown(profile, grouped)
        out2 = gen.render_markdown(profile, grouped)
        self.assertEqual(out1, out2)
        self.assertIn("## Software Development", out1)
        self.assertIn("[tool](https://github.com/kevinpinscoe/tool)", out1)

    def test_mermaid_is_deterministic_and_quoted(self):
        profile = make_profile()
        repos = [repo("tool", topics=["area-software-development"])]
        grouped = self._grouped(profile, repos)
        m1 = gen.render_mermaid(profile, grouped)
        m2 = gen.render_mermaid(profile, grouped)
        self.assertEqual(m1, m2)
        self.assertIn("```mermaid", m1)
        self.assertIn('"Software Development"', m1)

    def test_no_metrics_in_output(self):
        # No rendered metrics: no badges, metric cards, or activity graphs.
        # The only permitted image is the local knowledge-map SVG.
        profile = make_profile()
        repos = [repo("tool", topics=["area-software-development"])]
        out = gen.render_markdown(profile, self._grouped(profile, repos))
        for banned in ("shields.io", "img.shields", "star-history",
                       "github-readme-stats"):
            self.assertNotIn(banned, out.lower())
        # The knowledge map is embedded as the local SVG, and it is the only image.
        self.assertIn(f"![", out)
        self.assertIn(gen.KMAP_SVG_REF, out)
        self.assertEqual(out.count("!["), 1)

    def test_knowledge_map_dot_is_deterministic_and_radial(self):
        profile = make_profile()
        repos = [repo("tool", topics=["area-software-development"])]
        grouped = self._grouped(profile, repos)
        d1 = gen.render_graphviz_dot(profile, grouped)
        d2 = gen.render_graphviz_dot(profile, grouped)
        self.assertEqual(d1, d2)
        self.assertIn("layout=twopi", d1)
        self.assertIn('"Software Development"', d1)
        self.assertIn("root -- c0", d1)

    def test_markdown_escapes_pipe(self):
        profile = make_profile()
        repos = [repo("tool", description="a | b", topics=["area-software-development"])]
        out = gen.render_markdown(profile, self._grouped(profile, repos))
        self.assertIn("a \\| b", out)

    def test_preamble_stitched_and_replaces_summary(self):
        profile = make_profile()
        repos = [repo("tool", topics=["area-software-development"])]
        grouped = self._grouped(profile, repos)
        preamble = "# Kevin P. Inscoe\n\nPrincipal SWE and tool developer."
        out = gen.render_markdown(profile, grouped, preamble=preamble)
        self.assertTrue(out.startswith("# Kevin P. Inscoe"))
        self.assertIn("Principal SWE and tool developer.", out)
        # the auto summary line must not also appear
        self.assertNotIn(profile["summary"] + ".", out)
        # the map intro still follows
        self.assertIn("This page is a map", out)

    def test_no_preamble_uses_profile_summary(self):
        profile = make_profile()
        repos = [repo("tool", topics=["area-software-development"])]
        grouped = self._grouped(profile, repos)
        out = gen.render_markdown(profile, grouped, preamble=None)
        self.assertIn(profile["summary"] + ".", out)


class ValidationTests(unittest.TestCase):
    def test_duplicate_topic_rejected(self, ):
        import tempfile, os
        profile_text = (
            "owner: x\nname: X\nsummary: s\ncategories:\n"
            "  - {name: A, topic: area-dup}\n"
            "  - {name: B, topic: area-dup}\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
            fh.write(profile_text)
            path = Path(fh.name)
        try:
            with self.assertRaises(ValueError):
                gen.load_profile(path)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
