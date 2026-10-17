"""Refresh local profile graphs; keep the last valid SVG on provider failures."""

import argparse
from datetime import datetime, timedelta, timezone
from html import escape
from http.client import HTTPException
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


SVG = "{http://www.w3.org/2000/svg}"
SUMMARY_CARDS = {
    "profile-details": "Contributions on GitHub",
    "repos-per-language": "Top Languages by Repo",
    "most-commit-language": "Top Languages by Commit",
    "productive-time": "Commits (UTC +2.00)",
    "summary-stats": "Total Commits:",
}


def request_bytes(url, *, data=None, headers=None):
    request = Request(url, data=data, headers=headers or {})
    with urlopen(request, timeout=30) as response:
        content = response.read(2_000_001)
    if len(content) > 2_000_000:
        raise ValueError("Response exceeds 2 MB")
    return content


def validate_svg(content, expected_text):
    root = ET.fromstring(content)
    if root.tag != SVG + "svg":
        raise ValueError("Response is not an SVG")
    viewbox = [float(part) for part in root.get("viewBox", "").split()]
    if len(viewbox) != 4 or not all(0 < size < 10000 for size in viewbox[2:]):
        raise ValueError("SVG has no valid viewBox")
    text = " ".join("".join(node.itertext()) for node in root.iter(SVG + "text"))
    if expected_text not in text:
        raise ValueError("SVG does not contain the expected chart")


def fetch_summary(name, username):
    endpoint = "stats" if name == "summary-stats" else name
    options = {"username": username, "theme": "2077"}
    if name == "productive-time":
        options["utcOffset"] = "2"
    url = "https://github-profile-summary-cards.vercel.app/api/cards/"
    content = request_bytes(url + endpoint + "?" + urlencode(options))
    if name == "profile-details":
        root = ET.fromstring(content)
        heading = root.find(f"./{SVG}g/{SVG}text")
        if heading is None:
            raise ValueError("Profile card has no heading")
        heading.text = username
        ET.register_namespace("", SVG[1:-1])
        content = ET.tostring(root, encoding="utf-8")
    return content


def fetch_contributions(username, now):
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise ValueError("GH_TOKEN is required to fetch GitHub contributions")
    start = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
    query = """
      query($login: String!, $from: DateTime!, $to: DateTime!) {
        user(login: $login) {
          contributionsCollection(from: $from, to: $to) {
            contributionCalendar {
              weeks { contributionDays { date contributionCount } }
            }
          }
        }
      }
    """
    payload = {"query": query, "variables": {
        "login": username, "from": start.isoformat(), "to": now.isoformat(),
    }}
    response = json.loads(request_bytes(
        "https://api.github.com/graphql",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                 "User-Agent": "profile-graphs"},
    ))
    if response.get("errors") or not response.get("data", {}).get("user"):
        raise ValueError("GitHub did not return contribution data")
    weeks = response["data"]["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]
    expected = [(start + timedelta(days=i)).date().isoformat() for i in range(31)]
    days = sorted((day["date"], day["contributionCount"])
                  for week in weeks for day in week["contributionDays"]
                  if expected[0] <= day["date"] <= expected[-1])
    if [date for date, _ in days] != expected:
        raise ValueError("GitHub returned an incomplete contribution calendar")
    if any(type(count) is not int or count < 0 for _, count in days):
        raise ValueError("GitHub returned an invalid contribution count")
    return days


def render_activity(days, username):
    """Render the 31 daily counts without a third-party graph deployment."""
    maximum = max(1, max(count for _, count in days))
    points = [(60 + i * 980 / (len(days) - 1), 240 - count * 170 / maximum)
              for i, (_, count) in enumerate(days)]
    line = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="300" '
        'viewBox="0 0 1100 300" role="img" aria-labelledby="title desc">',
        f'<title id="title">{escape(username)}: daily GitHub contributions</title>',
        f'<desc id="desc">{days[0][0]} to {days[-1][0]}; '
        f'{sum(count for _, count in days)} contributions. Last day may be incomplete.</desc>',
        '<rect width="1100" height="300" rx="5" fill="#141321"/>',
        '<g font-family="Segoe UI, Ubuntu, sans-serif" fill="#03D8F3" font-size="13">',
        f'<text x="550" y="32" text-anchor="middle" font-size="22">'
        f'{escape(username)} Contribution Graph</text>',
    ]
    for fraction in (0, 0.5, 1):
        y = 240 - fraction * 170
        parts.append(f'<path d="M60 {y}H1040" stroke="#03D8F3" stroke-opacity="0.15"/>')
        parts.append(f'<text x="48" y="{y + 5}" text-anchor="end">{maximum * fraction:g}</text>')
    parts += [
        f'<polygon points="60,240 {line} 1040,240" fill="#FF0055" fill-opacity="0.15"/>',
        f'<polyline points="{line}" fill="none" stroke="#FF0055" stroke-width="2"/>',
    ]
    for (date, count), (x, y) in zip(days, points):
        parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="#FCEE0C">'
                     f'<title>{date}: {count} contributions</title></circle>')
    for i in (0, 5, 10, 15, 20, 25, 30):
        parts.append(f'<text x="{points[i][0]:.2f}" y="270" text-anchor="middle">'
                     f'{days[i][0][5:]}</text>')
    parts.append('</g></svg>')
    return "\n".join(parts).encode()


def refresh(path, fetch, expected_text):
    for attempt in range(3):
        try:
            content = fetch()
            validate_svg(content, expected_text)
            # Only replace the old snapshot once the whole response is validated.
            temporary = path.with_suffix(".svg.tmp")
            temporary.write_bytes(content)
            temporary.replace(path)
            print(f"Updated {path}")
            return True
        except (URLError, OSError, HTTPException, ValueError, ET.ParseError, KeyError, TypeError) as error:
            failure = str(error)
            if attempt < 2:
                time.sleep(2 ** attempt)
    try:
        validate_svg(path.read_bytes(), expected_text)
    except (OSError, ValueError, ET.ParseError):
        print(f"::error file={path}::Refresh failed and no valid snapshot exists: {failure}")
        return False
    print(f"::warning file={path}::Refresh failed; keeping the last valid snapshot: {failure}")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("profile"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    available = []
    for name, expected in SUMMARY_CARDS.items():
        available.append(refresh(args.output_dir / f"{name}.svg",
                                 lambda: fetch_summary(name, args.username), expected))
    available.append(refresh(
        args.output_dir / "activity-graph.svg",
        lambda: render_activity(fetch_contributions(args.username, now), args.username),
        "Contribution Graph",
    ))
    return 0 if all(available) else 1


if __name__ == "__main__":
    sys.exit(main())
