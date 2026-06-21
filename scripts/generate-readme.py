#!/usr/bin/env python3
"""Generate Kevin P. Inscoe's GitHub profile README from profile.yml.

The profile is a curated knowledge map and categorized project catalog. It
deliberately contains no popularity or activity metrics.

Local usage:

    # Install the one runtime dependency
    pip install -r requirements.txt

    # Generate README.md (uses an unauthenticated API by default; set a token
    # to avoid low anonymous rate limits)
    GITHUB_TOKEN=$(gh auth token) python scripts/generate-readme.py

    # Verify the committed README matches generated output (CI / pre-commit);
    # exits nonzero on drift and does not modify the file
    GITHUB_TOKEN=$(gh auth token) python scripts/generate-readme.py --check

profile.yml and preamble.md are the editable, human-maintained inputs.
preamble.md, when present, is stitched verbatim into the top of the output and
replaces the auto-generated name/summary. README.md is generated output.

API access (fetch_repositories) is kept separate from rendering so the
rendering logic can be unit-tested without any network calls.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = REPO_ROOT / "profile.yml"
PREAMBLE_PATH = REPO_ROOT / "preamble.md"
README_PATH = REPO_ROOT / "README.md"
ASSETS_DIR = REPO_ROOT / "assets"
KMAP_DOT_PATH = ASSETS_DIR / "knowledge-map.dot"
KMAP_SVG_PATH = ASSETS_DIR / "knowledge-map.svg"
# README-relative reference to the rendered map (kept in sync with KMAP_SVG_PATH).
KMAP_SVG_REF = "assets/knowledge-map.svg"

GENERATED_NOTICE = (
    "_This page is generated from `profile.yml` by `scripts/generate-readme.py`. "
    "Edit the configuration, not this file._"
)


# --------------------------------------------------------------------------- #
# Configuration loading and validation
# --------------------------------------------------------------------------- #
def load_profile(path: Path) -> dict:
    """Load and validate profile.yml. Raise ValueError on invalid config."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise ValueError(f"profile config not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"profile config is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("profile config must be a YAML mapping")

    for key in ("owner", "name", "summary", "categories"):
        if key not in data:
            raise ValueError(f"profile config missing required key: {key}")

    categories = data["categories"]
    if not isinstance(categories, list) or not categories:
        raise ValueError("profile config 'categories' must be a non-empty list")

    seen_topics = set()
    for cat in categories:
        if not isinstance(cat, dict) or "name" not in cat or "topic" not in cat:
            raise ValueError("each category needs a 'name' and a 'topic'")
        topic = cat["topic"]
        if topic in seen_topics:
            raise ValueError(f"duplicate category topic: {topic}")
        seen_topics.add(topic)

    data.setdefault("repository_defaults", {})
    data.setdefault("repositories", {})
    repos = data["repositories"]
    repos.setdefault("include", [])
    repos.setdefault("exclude", [])
    repos.setdefault("categories", {})
    repos.setdefault("overrides", {})
    return data


def known_area_topics(profile: dict) -> set:
    """All controlled area-* topics declared by the profile's categories."""
    return {cat["topic"] for cat in profile["categories"]}


def load_preamble(path: Path) -> str | None:
    """Return the human-curated preamble Markdown, or None if absent/empty.

    The preamble is trusted, hand-written Markdown stitched into the front of
    the generated README. It is never generated.
    """
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


# --------------------------------------------------------------------------- #
# GitHub API access (the only part that touches the network)
# --------------------------------------------------------------------------- #
def fetch_repositories(owner: str, token: str | None = None) -> list[dict]:
    """Return all public repositories for owner, following pagination.

    Each item is normalized to: name, description, url, topics, archived, fork.
    """
    per_page = 100
    page = 1
    results: list[dict] = []
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{owner}-profile-generator",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    while True:
        url = (
            f"https://api.github.com/users/{owner}/repos"
            f"?per_page={per_page}&page={page}&type=owner&sort=full_name"
        )
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                batch = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError(
                f"GitHub API request failed ({exc.code}) for {url}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GitHub API request failed for {url}: {exc}") from exc

        if not isinstance(batch, list):
            raise RuntimeError(f"unexpected GitHub API response for {url}")
        if not batch:
            break

        for repo in batch:
            results.append(
                {
                    "name": repo.get("name", ""),
                    "description": repo.get("description") or "",
                    "url": repo.get("html_url", ""),
                    "topics": list(repo.get("topics") or []),
                    "archived": bool(repo.get("archived")),
                    "fork": bool(repo.get("fork")),
                }
            )

        if len(batch) < per_page:
            break
        page += 1

    return results


# --------------------------------------------------------------------------- #
# Pure selection / grouping logic (no network — unit-testable)
# --------------------------------------------------------------------------- #
def effective_area_topics(repo: dict, profile: dict) -> set:
    """Area topics for a repo: GitHub area-* topics UNION profile.yml map."""
    area_topics = known_area_topics(profile)
    from_github = {t for t in repo.get("topics", []) if t in area_topics}
    mapped = profile["repositories"]["categories"].get(repo["name"], [])
    from_map = {t for t in mapped if t in area_topics}
    return from_github | from_map


def select_repositories(repos: list[dict], profile: dict, warn=None) -> list[dict]:
    """Apply exclusion, inclusion, default filters, and area-topic requirement.

    Returns the kept repos. `warn` is an optional callable for omission notices.
    """
    if warn is None:
        warn = lambda msg: print(f"warning: {msg}", file=sys.stderr)

    repo_cfg = profile["repositories"]
    exclude = set(repo_cfg["exclude"])
    include = set(repo_cfg["include"])
    defaults = profile["repository_defaults"]
    include_archived = bool(defaults.get("include_archived", False))
    include_forks = bool(defaults.get("include_forks", False))

    selected = []
    for repo in repos:
        name = repo["name"]
        if name in exclude:
            continue
        if name not in include:
            if repo["archived"] and not include_archived:
                continue
            if repo["fork"] and not include_forks:
                continue
        if not effective_area_topics(repo, profile):
            warn(f"omitting '{name}': no configured area topic")
            continue
        selected.append(repo)
    return selected


def _sort_key(render: dict):
    order = render["order"]
    # Repos with an explicit numeric order come first (sorted by order),
    # then the rest alphabetically by name.
    return (order is None, order if order is not None else 0, render["name"].lower())


def group_by_category(selected: list[dict], profile: dict) -> list[dict]:
    """Return categories in configured order, each with its sorted repos.

    A repo with multiple area topics appears under each matching category.
    Only categories that contain at least one repo are returned.
    """
    overrides = profile["repositories"]["overrides"]
    grouped = []
    for cat in profile["categories"]:
        topic = cat["topic"]
        renders = []
        for repo in selected:
            if topic not in effective_area_topics(repo, profile):
                continue
            ov = overrides.get(repo["name"], {})
            description = ov.get("description") or repo["description"] or ""
            renders.append(
                {
                    "name": repo["name"],
                    "url": repo["url"],
                    "description": description.strip() or "No description provided.",
                    "topics": sorted(
                        t
                        for t in repo.get("topics", [])
                        if t not in known_area_topics(profile)
                    ),
                    "order": ov.get("order"),
                }
            )
        if renders:
            renders.sort(key=_sort_key)
            grouped.append({"category": cat, "repos": renders})
    return grouped


# --------------------------------------------------------------------------- #
# Rendering (pure)
# --------------------------------------------------------------------------- #
def _mermaid_label(text: str) -> str:
    """Make text safe inside a quoted Mermaid node label."""
    return text.replace('"', "'").replace("\n", " ").strip()


def _md_escape(text: str) -> str:
    """Neutralize characters that could break inline Markdown."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def render_mermaid(profile: dict, grouped: list[dict]) -> str:
    """Deterministic Mermaid knowledge-area diagram (only non-empty areas)."""
    lines = ["```mermaid", "graph TD"]
    root = "profile"
    lines.append(f'    {root}["{_mermaid_label(profile["name"])}"]')
    for i, group in enumerate(grouped):
        cat = group["category"]
        cid = f"c{i}"
        lines.append(f'    {root} --> {cid}["{_mermaid_label(cat["name"])}"]')
        for j, subject in enumerate(cat.get("subjects", []) or []):
            sid = f"{cid}s{j}"
            lines.append(f'    {cid} --> {sid}["{_mermaid_label(subject)}"]')
    lines.append("```")
    return "\n".join(lines)


def _dot_label(text: str) -> str:
    """Make text safe inside a quoted Graphviz label."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()


def render_graphviz_dot(profile: dict, grouped: list[dict]) -> str:
    """Deterministic Graphviz DOT for a radial (twopi) knowledge map.

    A top-down tree of ~14 categories fans out into an unreadably wide diagram;
    a radial hub-and-spoke layout keeps the same information compact. Rendered to
    SVG with `twopi`. Pure: returns DOT text and performs no I/O.
    """
    lines = [
        "// Generated by scripts/generate-readme.py — edit profile.yml, not this file.",
        "graph knowledge_map {",
        "    layout=twopi;",
        "    overlap=false;",
        "    splines=true;",
        "    ranksep=2.2;",
        '    bgcolor="transparent";',
        '    node [shape=box style="rounded,filled" fontname="Helvetica" fontsize=11];',
        '    edge [color="#94a3b8"];',
        (
            f'    root [label="{_dot_label(profile["name"])}" shape=ellipse '
            'fillcolor="#222222" fontcolor="white" fontsize=16];'
        ),
    ]
    for i, group in enumerate(grouped):
        cat = group["category"]
        cid = f"c{i}"
        lines.append(
            f'    {cid} [label="{_dot_label(cat["name"])}" '
            'fillcolor="#2b6cb0" fontcolor="white"];'
        )
        lines.append(f"    root -- {cid};")
        for j, subject in enumerate(cat.get("subjects", []) or []):
            sid = f"{cid}s{j}"
            lines.append(
                f'    {sid} [label="{_dot_label(subject)}" fillcolor="#e2e8f0"];'
            )
            lines.append(f"    {cid} -- {sid};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def write_knowledge_map(profile: dict, grouped: list[dict]) -> None:
    """Write the DOT source and render it to SVG with Graphviz `twopi`.

    Raises RuntimeError if Graphviz is unavailable or rendering fails, so a
    broken map surfaces loudly rather than leaving a stale SVG in place.
    """
    twopi = shutil.which("twopi")
    if not twopi:
        raise RuntimeError(
            "Graphviz 'twopi' not found on PATH — install Graphviz "
            "(e.g. 'sudo dnf install graphviz') to render the knowledge map"
        )
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    dot = render_graphviz_dot(profile, grouped)
    KMAP_DOT_PATH.write_text(dot, encoding="utf-8")
    try:
        result = subprocess.run(
            [twopi, "-Tsvg", "-o", str(KMAP_SVG_PATH), str(KMAP_DOT_PATH)],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise RuntimeError(f"failed to run twopi: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"twopi failed: {result.stderr.strip()}")


def render_markdown(profile: dict, grouped: list[dict], preamble: str | None = None) -> str:
    """Render the full deterministic profile README.

    When `preamble` is provided, it is stitched in verbatim at the very top and
    replaces the auto-generated name heading and summary line. Otherwise the
    name and summary are derived from profile.yml.
    """
    parts = []
    if preamble:
        parts.append(preamble.strip())
        parts.append("")
    else:
        parts.append(f"# {profile['name']}")
        parts.append("")
        parts.append(f"{profile['summary']}.")
        parts.append("")
    parts.append(
        "This page is a map of my work and interests — a curated knowledge "
        "map and categorized project catalog, not a performance dashboard. "
        "It does not track stars, forks, followers, or activity."
    )
    parts.append("")
    parts.append("## Knowledge map")
    parts.append("")
    parts.append(
        f"![Knowledge map of {_md_escape(profile['name'])}'s work and interests]"
        f"({KMAP_SVG_REF})"
    )
    parts.append("")

    for group in grouped:
        cat = group["category"]
        parts.append(f"## {cat['name']}")
        parts.append("")
        if cat.get("description"):
            parts.append(_md_escape(cat["description"]))
            parts.append("")
        for repo in group["repos"]:
            line = f"- [{repo['name']}]({repo['url']}) — {_md_escape(repo['description'])}"
            if repo["topics"]:
                tags = ", ".join(f"`{t}`" for t in repo["topics"])
                line += f" ({tags})"
            parts.append(line)
        parts.append("")

    parts.append("---")
    parts.append("")
    parts.append(GENERATED_NOTICE)
    parts.append("")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def build_readme(profile: dict, repos: list[dict], preamble: str | None = None) -> str:
    selected = select_repositories(repos, profile)
    grouped = group_by_category(selected, profile)
    if not grouped:
        raise RuntimeError(
            "no repositories matched any configured area topic — "
            "check profile.yml 'categories' map and GitHub topics"
        )
    return render_markdown(profile, grouped, preamble)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the profile README.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit nonzero if README.md differs from generated output; do not write",
    )
    args = parser.parse_args(argv)

    try:
        profile = load_profile(PROFILE_PATH)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    preamble = load_preamble(PREAMBLE_PATH)
    token = os.environ.get("GITHUB_TOKEN")
    try:
        repos = fetch_repositories(profile["owner"], token)
        selected = select_repositories(repos, profile)
        grouped = group_by_category(selected, profile)
        if not grouped:
            raise RuntimeError(
                "no repositories matched any configured area topic — "
                "check profile.yml 'categories' map and GitHub topics"
            )
        content = render_markdown(profile, grouped, preamble)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.check:
        current = README_PATH.read_text(encoding="utf-8") if README_PATH.exists() else ""
        if current != content:
            print("error: README.md is out of date; run the generator", file=sys.stderr)
            return 1
        print("README.md is up to date.")
        return 0

    try:
        write_knowledge_map(profile, grouped)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {KMAP_SVG_PATH}")

    existing = README_PATH.read_text(encoding="utf-8") if README_PATH.exists() else None
    if existing == content:
        print("README.md unchanged.")
        return 0
    README_PATH.write_text(content, encoding="utf-8")
    print(f"Wrote {README_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
