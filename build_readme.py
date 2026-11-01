#!/usr/bin/env python3
"""Regenerate the auto-updating sections of the profile README.

Content sources (all public, no credentials required beyond a rate-limit token):
  - Posts:  https://blog.qfpqhyl.top/atom.xml (Hexo feed)
  - Tweets: the daily snapshot committed by ox0pine.github.io
            (tools/fetch-twitter.js + .github/workflows/twitter.yml)
  - Stats:  GitHub REST API

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
TWITTER_SNAPSHOT = (
    "https://raw.githubusercontent.com/ox0pine/ox0pine.github.io/main/source/_data/twitter.json"
)
GITHUB_USER = "ox0pine"
PROFILE_REPO = "ox0pine"

MAX_ITEMS = 6
ITEM_MAX_LEN = 22

TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
ATOM = {"a": "http://www.w3.org/2005/Atom"}
TIMEOUT = 30


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


def replace_chunk(content: str, marker: str, chunk: str, inline: bool = False) -> str:
    pattern = re.compile(
        r"<!-- {} starts -->.*?<!-- {} ends -->".format(marker, marker),
        re.DOTALL,
    )
    # A marker sharing its line with markdown makes GFM emit that line as an HTML
    # block, so `[text](url)` stays literal. Block chunks therefore get their own lines.
    body = chunk if inline else f"\n{chunk}\n"
    block = f"<!-- {marker} starts -->{body}<!-- {marker} ends -->"
    replaced, count = pattern.subn(lambda _: block, content)
    if not count:
        print(f"warning: marker '{marker}' not found in README.md", flush=True)
        return content
    return replaced


def collapse(text: str | None) -> str:
    # U+FFFC is X's media placeholder; the rest are zero-width/formatting noise.
    cleaned = re.sub("[\ufffc\u200b\u200e\u200f\ufeff\u2060]", "", text or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def truncate(text: str, max_len: int) -> str:
    """Keep one ellipsis instead of letting a long line wrap."""
    text = collapse(text)
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip(" ，、,；;·-") + "…"


def link_text(text: str) -> str:
    """Escape what would break a markdown link inside a table cell."""
    return (
        text.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("|", "丨")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# --------------------------------------------------------------------------- #
# blog
# --------------------------------------------------------------------------- #


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
        if len(posts) >= MAX_ITEMS:
            break
    return posts


def posts_chunk(posts: list[dict[str, str]]) -> str:
    # Dates stay out of the rendered list; the feed order already carries recency.
    lines = [
        "• [{}]({})".format(truncate(post["title"], ITEM_MAX_LEN), post["url"])
        for post in posts
    ]
    return "<br>".join(lines)


# --------------------------------------------------------------------------- #
# tweets
# --------------------------------------------------------------------------- #


def fetch_tweets() -> list[dict[str, str]]:
    payload = fetch_json(TWITTER_SNAPSHOT)
    tweets = []
    for tweet in payload.get("tweets") or []:
        url = collapse(tweet.get("url"))
        created = collapse(tweet.get("createdAt"))
        if not (url and created):
            continue
        text = collapse(tweet.get("text"))
        if not text:
            count = len(tweet.get("media") or [])
            text = f"（{count} 张图）" if count > 1 else "（图片）"
        tweets.append({"url": url, "text": text, "date": created[:10]})
    tweets.sort(key=lambda tweet: tweet["date"], reverse=True)
    return tweets[:MAX_ITEMS]


def tweets_chunk(tweets: list[dict[str, str]]) -> str:
    lines = [
        "• [{}]({})".format(link_text(truncate(tweet["text"], ITEM_MAX_LEN)), tweet["url"])
        for tweet in tweets
    ]
    return "<br>".join(lines)


# --------------------------------------------------------------------------- #
# github
# --------------------------------------------------------------------------- #


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
        tweets = fetch_tweets()
        if tweets:
            content = replace_chunk(content, "tweets", tweets_chunk(tweets))
        else:
            print("warning: tweet snapshot empty, keeping existing content", flush=True)
            failures += 1
    except (urllib.error.URLError, ValueError) as error:
        print(f"warning: tweet snapshot failed: {error}", flush=True)
        failures += 1

    try:
        repos = fetch_repos()
        if repos:
            user = fetch_json(f"https://api.github.com/users/{GITHUB_USER}")
            content = replace_chunk(content, "github_stats", stats_chunk(repos, user), inline=True)
        else:
            print("warning: no repositories returned", flush=True)
            failures += 1
    except (urllib.error.URLError, ValueError) as error:
        print(f"warning: github api failed: {error}", flush=True)
        failures += 1

    if failures == 3:
        print("error: no source refreshed, README left untouched", flush=True)
        return 1

    README.write_text(content, encoding="utf-8")
    print("README.md updated", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
