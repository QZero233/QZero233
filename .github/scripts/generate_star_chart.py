#!/usr/bin/env python3
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter

OWNER = os.getenv("REPO_OWNER", "QZero233")
REPO = os.getenv("REPO_NAME", "QZero233")
DAYS = int(os.getenv("STAR_CHART_DAYS", "365"))
OUTPUT = os.getenv("STAR_CHART_OUTPUT", "assets/star-history.svg")
STARGAZERS_ACCEPT = "application/vnd.github.v3.star+json"


def github_request(url: str, accept: str = STARGAZERS_ACCEPT):
    """Request GitHub API data using GH_TOKEN first, then GITHUB_TOKEN as fallback."""
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"{OWNER}-{REPO}-star-chart-action",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        url,
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        link = resp.headers.get("Link", "")
        data = json.load(resp)
    return data, link


def parse_next_link(link_header: str):
    if not link_header:
        return None
    parts = [p.strip() for p in link_header.split(",")]
    for part in parts:
        if 'rel="next"' in part:
            start = part.find("<")
            end = part.find(">")
            if start != -1 and end != -1:
                return part[start + 1 : end]
    return None


def fetch_paginated(url: str, accept: str):
    rows = []
    while url:
        data, link = github_request(url, accept=accept)
        if not isinstance(data, list):
            break
        rows.extend(data)
        url = parse_next_link(link)
    return rows


def fetch_public_repos(owner: str):
    per_page = 100
    user_url = f"https://api.github.com/users/{owner}/repos?type=public&per_page={per_page}&page=1"
    org_url = f"https://api.github.com/orgs/{owner}/repos?type=public&per_page={per_page}&page=1"
    accept = "application/vnd.github+json"
    try:
        rows = fetch_paginated(user_url, accept=accept)
        if rows:
            return [row.get("name") for row in rows if row.get("name")]
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            print(f"Warning: failed to list user repos for {owner}: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"Warning: failed to list user repos for {owner}: {exc}", file=sys.stderr)
    try:
        rows = fetch_paginated(org_url, accept=accept)
        return [row.get("name") for row in rows if row.get("name")]
    except Exception as exc:
        print(f"Warning: failed to list org repos for {owner}: {exc}", file=sys.stderr)
        return []


def fetch_repo_star_dates(owner: str, repo: str):
    per_page = 100
    url = f"https://api.github.com/repos/{owner}/{repo}/stargazers?per_page={per_page}&page=1"
    dates = []
    while url:
        rows, link = github_request(url, accept=STARGAZERS_ACCEPT)
        if not isinstance(rows, list):
            raise RuntimeError(f"unexpected stargazers response type for {owner}/{repo}")
        for row in rows:
            starred_at = row.get("starred_at")
            if starred_at:
                dates.append(dt.datetime.fromisoformat(starred_at.replace("Z", "+00:00")).date())
        url = parse_next_link(link)
    return sorted(dates)


def fetch_star_dates(owner: str):
    repos = fetch_public_repos(owner)
    dates = []
    repos_with_star_dates = 0
    for repo in repos:
        repo_dates = fetch_repo_star_dates(owner, repo)
        if repo_dates:
            repos_with_star_dates += 1
        dates.extend(repo_dates)
    print(
        f"Fetched {len(dates)} stargazer events from {len(repos)} public repos "
        f"({repos_with_star_dates} repos with timestamped stars)",
        file=sys.stderr,
    )
    return sorted(dates)


def build_series(star_dates, days):
    today = dt.date.today()
    start = today - dt.timedelta(days=days - 1)
    by_day = Counter(star_dates)

    before = sum(1 for d in star_dates if d < start)

    x_dates = []
    y_values = []
    current = before
    day = start
    while day <= today:
        current += by_day.get(day, 0)
        x_dates.append(day)
        y_values.append(current)
        day += dt.timedelta(days=1)
    return x_dates, y_values


def esc(text: str):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_svg(x_dates, y_values, owner):
    width, height = 980, 360
    pad_l, pad_r, pad_t, pad_b = 64, 24, 48, 56
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    if not y_values:
        y_values = [0]
        x_dates = [dt.date.today()]

    min_y = 0
    max_y = max(y_values)
    if max_y <= min_y:
        max_y = min_y + 1

    def px(i):
        n = max(1, len(y_values) - 1)
        return pad_l + (i / n) * plot_w

    def py(v):
        return pad_t + (1 - (v - min_y) / (max_y - min_y)) * plot_h

    points = " ".join(f"{px(i):.2f},{py(v):.2f}" for i, v in enumerate(y_values))

    y_ticks = 5
    tick_vals = [min_y + (max_y - min_y) * i / y_ticks for i in range(y_ticks + 1)]

    def format_tick(v: float) -> str:
        if abs(v - round(v)) < 1e-9:
            return str(int(round(v)))
        return f"{v:.1f}"

    x_tick_count = 6
    x_tick_idx = sorted(set(round((len(x_dates) - 1) * i / x_tick_count) for i in range(x_tick_count + 1)))

    title = f"{owner} Public Repos Star History (Last {len(x_dates)} Days)"

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<defs><style><![CDATA[text{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif;fill:#8b949e} .title{font-size:20px;fill:#c9d1d9;font-weight:600} .axis{font-size:12px} .value{font-size:12px}]]></style></defs>',
        '<rect width="100%" height="100%" fill="#0d1117"/>',
        f'<text x="{pad_l}" y="30" class="title">{esc(title)}</text>',
        f'<text x="{width - pad_r}" y="30" text-anchor="end" class="value">Updated: {esc(dt.date.today().isoformat())}</text>',
    ]

    for tv in tick_vals:
        y = py(tv)
        lines.append(f'<line x1="{pad_l}" y1="{y:.2f}" x2="{width-pad_r}" y2="{y:.2f}" stroke="#30363d" stroke-width="1"/>')
        lines.append(f'<text x="{pad_l-8}" y="{y+4:.2f}" text-anchor="end" class="axis">{format_tick(tv)}</text>')

    for idx in x_tick_idx:
        x = px(idx)
        label = x_dates[idx].isoformat()
        lines.append(f'<line x1="{x:.2f}" y1="{pad_t}" x2="{x:.2f}" y2="{height-pad_b}" stroke="#21262d" stroke-width="1"/>')
        lines.append(f'<text x="{x:.2f}" y="{height-pad_b+22}" text-anchor="middle" class="axis">{esc(label)}</text>')

    lines.append(f'<polyline fill="none" stroke="#58a6ff" stroke-width="3" points="{points}"/>')
    lines.append(f'<circle cx="{px(len(y_values)-1):.2f}" cy="{py(y_values[-1]):.2f}" r="4" fill="#58a6ff"/>')
    lines.append(f'<text x="{px(len(y_values)-1):.2f}" y="{py(y_values[-1]) - 10:.2f}" text-anchor="middle" class="value">{y_values[-1]}</text>')
    lines.append('</svg>')
    return "\n".join(lines)


def main():
    owner = OWNER
    try:
        star_dates = fetch_star_dates(owner)
    except Exception as exc:
        print(f"Warning: failed to fetch stargazer history: {exc}", file=sys.stderr)
        star_dates = []
    x_dates, y_values = build_series(star_dates, DAYS)
    svg = render_svg(x_dates, y_values, owner)

    out_path = OUTPUT
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(svg)

    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
