#!/usr/bin/env python3
"""Regenerate the auto-updating sections of the profile README.

Content sources:
  - Posts:   https://blog.qfpqhyl.top/atom.xml (Hexo feed)
  - Projects: GitHub REST API, public data only

Only the regions between matching `<!-- name starts --> ... <!-- name ends -->`
markers are rewritten. Any source that fails to load keeps its previous content,
so a bad feed or an API outage never blanks the profile.

Set GH_TOKEN (or GITHUB_TOKEN) to raise the GitHub API rate limit; public data
works without it.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
README = ROOT / "README.md"

BLOG_FEED = "https://blog.qfpqhyl.top/atom.xml"
BLOG_BASE = urlsplit("https://blog.qfpqhyl.top")
GITHUB_USER = "ox0pine"
PROFILE_REPO = "ox0pine"

MAX_POSTS = 6
MAX_PROJECTS = 6
TITLE_MAX_LEN = 36
DESC_MAX_LEN = 40

TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
ATOM = {"a": "http://www.w3.org/2005/Atom"}
TIMEOUT = 30

EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # pictographs, symbols, transport, supplemental
    "\u2600-\u27bf"          # misc symbols + dingbats
    "\u2b00-\u2bff"          # arrows / stars
    "\ufe0f\u200d"           # variation selector-16, zero width joiner
    "]+",
    flags=re.UNICODE,
)


def http_get(url: str) -> bytes:
    headers = {"User-Agent": f"{GITHUB_USER}-profile-readme"}
    if TOKEN and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {TOKEN}"
        headers["Accept"] = "application/vnd.github+json"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def fetch_json(url: str):
    return json.loads(http_get(url).decode("utf-8"))


def replace_chunk(content: str, marker: str, chunk: str) -> str:
    pattern = re.compile(
        r"<!-- {} starts -->.*?<!-- {} ends -->".format(marker, marker),
        re.DOTALL,
    )
    block = f"<!-- {marker} starts -->{chunk}<!-- {marker} ends -->"
    replaced, count = pattern.subn(lambda _: block, content)
    if not count:
        print(f"warning: marker '{marker}' not found in README.md", flush=True)
        return content
    return replaced


def collapse(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def truncate_middle(text: str, max_len: int) -> str:
    text = collapse(text)
    if len(text) <= max_len:
        return text
    keep = max_len - 3
    if keep <= 1:
        return text[:max_len]
    return f"{text[: (keep + 1) // 2]}...{text[-(keep // 2):]}"


def truncate_end(text: str, max_len: int) -> str:
    text = collapse(text)
    if len(text) <= max_len:
        return text
    trimmed = text[: max_len - 3].rstrip(" ，、,；;·-")
    return f"{trimmed}..."


def strip_emoji(text: str) -> str:
    return collapse(EMOJI_RE.sub(" ", text))


def project_desc(raw: str) -> str:
    """Bilingual repo descriptions carry a mirrored translation after '|'."""
    text = strip_emoji(raw)
    head = text.split("|")[0].strip()
    return truncate_end(head or text, DESC_MAX_LEN)


def canonical_blog_url(raw: str) -> str:
    """Feed links still point at the old *.github.io host; rewrite to the blog domain."""
    parts = urlsplit(raw)
    if not parts.netloc or parts.netloc == BLOG_BASE.netloc:
        return raw
    return urlunsplit((BLOG_BASE.scheme, BLOG_BASE.netloc, parts.path, parts.query, parts.fragment))


def date_of(entry: ET.Element) -> str:
    # Hexo rewrites <updated> on every build; <published> is the real post date.
    for tag in ("published", "updated"):
        node = entry.find(f"a:{tag}", ATOM)
        if node is not None and node.text and len(node.text) >= 10:
            return node.text[:10]
    return ""


def fetch_posts() -> list[dict[str, str]]:
    root = ET.fromstring(http_get(BLOG_FEED))
    posts: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in root.findall("a:entry", ATOM):
        title = collapse(entry.findtext("a:title", default="", namespaces=ATOM))
        link = next(
            (
                node
                for node in entry.findall("a:link", ATOM)
                if node.get("rel", "alternate") == "alternate"
            ),
            None,
        )
        url = canonical_blog_url(link.get("href", "")) if link is not None else ""
        date = date_of(entry)
        if not (title and url and date) or url in seen:
            continue
        seen.add(url)
        posts.append({"title": title, "url": url, "date": date})
        if len(posts) >= MAX_POSTS:
            break
    return posts


def fetch_repos() -> list[dict]:
    url = f"https://api.github.com/users/{GITHUB_USER}/repos?per_page=100&sort=pushed"
    repos = fetch_json(url)
    if not isinstance(repos, list):
        return []
    return [
        repo
        for repo in repos
        if not repo.get("fork") and repo.get("name") != PROFILE_REPO
    ]


def posts_chunk(posts: list[dict[str, str]]) -> str:
    lines = [
        "• [{}]({}) - {}".format(truncate_middle(post["title"], TITLE_MAX_LEN), post["url"], post["date"])
        for post in posts
    ]
    return "<br>".join(lines)


def projects_chunk(repos: list[dict]) -> str:
    ranked = sorted(
        repos,
        key=lambda repo: (repo.get("stargazers_count", 0), repo.get("pushed_at", "")),
        reverse=True,
    )
    lines = []
    for repo in ranked[:MAX_PROJECTS]:
        name = repo["name"]
        desc = project_desc(repo.get("description") or "")
        line = f"• [{name}]({repo['html_url']}) ★{repo.get('stargazers_count', 0)}"
        lines.append(f"{line} - {desc}" if desc else line)
    return "<br>".join(lines)


def stats_chunk(repos: list[dict], user: dict) -> str:
    stars = sum(repo.get("stargazers_count", 0) for repo in repos)
    forks = sum(repo.get("forks_count", 0) for repo in repos)
    return "{} followers, {} stars, {} forks".format(
        f"{user.get('followers', 0):,}", f"{stars:,}", f"{forks:,}"
    )


def main() -> int:
    content = README.read_text(encoding="utf-8")
    failures = 0

    try:
        posts = fetch_posts()
        if posts:
            content = replace_chunk(content, "blog", posts_chunk(posts))
        else:
            print("warning: no posts in feed, keeping existing content", flush=True)
            failures += 1
    except (urllib.error.URLError, ET.ParseError, ValueError) as error:
        print(f"warning: blog feed failed: {error}", flush=True)
        failures += 1

    try:
        repos = fetch_repos()
        if repos:
            content = replace_chunk(content, "projects", projects_chunk(repos))
            user = fetch_json(f"https://api.github.com/users/{GITHUB_USER}")
            content = replace_chunk(content, "github_stats", stats_chunk(repos, user))
        else:
            print("warning: no repositories returned", flush=True)
            failures += 1
    except (urllib.error.URLError, ValueError) as error:
        print(f"warning: github api failed: {error}", flush=True)
        failures += 1

    if failures == 2:
        print("error: no source refreshed, README left untouched", flush=True)
        return 1

    README.write_text(content, encoding="utf-8")
    print("README.md updated", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
