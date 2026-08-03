#!/usr/bin/env python3
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter

OWNER = os.getenv("REPO_OWNER", "QZero233")
REPO = os.getenv("REPO_NAME", "QZero233")
START_DATE_STR = os.getenv("STAR_CHART_START_DATE", "2025-01-01")
OUTPUT = os.getenv("STAR_CHART_OUTPUT", "assets/star-history.svg")
# Stop early once we are about to exhaust the quota so we keep partial data
# instead of burning the last request and then failing everything.
RATE_LIMIT_FLOOR = 2
STARGAZERS_ACCEPTS = (
    "application/vnd.github.star+json",
    "application/vnd.github.v3.star+json",
)


class RateLimitError(Exception):
    """Raised when the GitHub API rate limit is (about to be) exhausted."""

    def __init__(self, reset_epoch: int = 0):
        super().__init__("GitHub API rate limit reached")
        self.reset_epoch = reset_epoch


def github_request(url: str, accept: str = STARGAZERS_ACCEPTS[0]):
    """Request GitHub API data.

    Token priority: STAR_CHART_TOKEN (dedicated PAT) -> GH_TOKEN -> GITHUB_TOKEN.
    Raises RateLimitError when the remaining quota drops to the floor so the
    caller can stop gracefully instead of failing every remaining request.
    """
    token = os.getenv("STAR_CHART_TOKEN") or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"{OWNER}-{REPO}-star-chart-action",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        if exc.code in (403, 429) and "rate limit" in body.lower():
            raise RateLimitError(0)
        raise
    link = resp.headers.get("Link", "")
    remaining = resp.headers.get("X-RateLimit-Remaining")
    reset = resp.headers.get("X-RateLimit-Reset")
    if remaining is not None:
        try:
            if int(remaining) <= RATE_LIMIT_FLOOR:
                raise RateLimitError(int(reset) if reset else 0)
        except ValueError:
            pass
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
    except RateLimitError:
        raise
    except Exception as exc:
        print(f"Warning: failed to list user repos for {owner}: {exc}", file=sys.stderr)
    try:
        rows = fetch_paginated(org_url, accept=accept)
        return [row.get("name") for row in rows if row.get("name")]
    except RateLimitError:
        raise
    except Exception as exc:
        print(f"Warning: failed to list org repos for {owner}: {exc}", file=sys.stderr)
        return []


def fetch_repo_star_dates(owner: str, repo: str):
    per_page = 100
    initial_url = f"https://api.github.com/repos/{owner}/{repo}/stargazers?per_page={per_page}&page=1"
    for accept in STARGAZERS_ACCEPTS:
        url = initial_url
        dates = []
        saw_rows = False
        saw_starred_at = False
        while url:
            rows, link = github_request(url, accept=accept)
            if not isinstance(rows, list):
                raise RuntimeError(
                    f"unexpected stargazers response type for {owner}/{repo}: {type(rows).__name__}"
                )
            if rows:
                saw_rows = True
            for row in rows:
                starred_at = row.get("starred_at")
                if starred_at:
                    saw_starred_at = True
                    dates.append(dt.datetime.fromisoformat(starred_at.replace("Z", "+00:00")).date())
            url = parse_next_link(link)
        if saw_starred_at or not saw_rows:
            return sorted(dates)
    raise RuntimeError(f"no starred_at field returned for {owner}/{repo}")


def fetch_star_dates(owner: str):
    repos = fetch_public_repos(owner)
    dates = []
    had_errors = False
    repos_with_star_dates = 0
    for repo in repos:
        try:
            repo_dates = fetch_repo_star_dates(owner, repo)
        except RateLimitError:
            # Stop the whole run; propagate so main() can preserve last good chart.
            raise
        except Exception as exc:
            print(f"Warning: failed to fetch stars for {owner}/{repo}: {exc}", file=sys.stderr)
            had_errors = True
            continue
        if repo_dates:
            repos_with_star_dates += 1
        dates.extend(repo_dates)
    print(
        f"Fetched {len(dates)} stargazer events from {len(repos)} public repos "
        f"({repos_with_star_dates} repos with timestamped stars)",
        file=sys.stderr,
    )
    return sorted(dates), had_errors


def build_series(star_dates, start_date: dt.date):
    today = dt.date.today()
    start = start_date
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


def _nice_max(value: float) -> float:
    """Round up to a nice round number for the Y axis ceiling."""
    if value <= 0:
        return 1
    magnitude = 10 ** (len(str(int(value))) - 1)
    if value <= magnitude:
        return magnitude
    if value <= 2 * magnitude:
        return 2 * magnitude
    if value <= 5 * magnitude:
        return 5 * magnitude
    return 10 * magnitude


def render_svg(x_dates, y_values):
    width, height = 980, 360
    pad_l, pad_r, pad_t, pad_b = 56, 28, 40, 52
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    if not y_values:
        y_values = [0]
        x_dates = [dt.date.today()]

    final_value = y_values[-1]
    nice_top = _nice_max(final_value)

    def px(i):
        n = max(1, len(y_values) - 1)
        return pad_l + (i / n) * plot_w

    def py(v):
        return pad_t + (1 - v / nice_top) * plot_h

    points = " ".join(f"{px(i):.2f},{py(v):.2f}" for i, v in enumerate(y_values))
    area_path = (
        f"M{px(0):.2f},{py(y_values[0]):.2f} "
        + " ".join(f"L{px(i):.2f},{py(v):.2f}" for i, v in enumerate(y_values))
        + f" L{px(len(y_values)-1):.2f},{(pad_t+plot_h):.2f} L{px(0):.2f},{(pad_t+plot_h):.2f} Z"
    )

    y_ticks = 5
    tick_vals = [nice_top * i / y_ticks for i in range(y_ticks + 1)]

    x_tick_count = 6
    x_tick_idx = sorted(set(round((len(x_dates) - 1) * i / x_tick_count) for i in range(x_tick_count + 1)))

    MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    def fmt_x(d):
        return f"{MONTHS[d.month]} ’{d.year % 100:02d}"

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<defs>',
        '  <linearGradient id="area" x1="0" y1="0" x2="0" y2="1">',
        '    <stop offset="0%" stop-color="#58a6ff" stop-opacity="0.35"/>',
        '    <stop offset="100%" stop-color="#58a6ff" stop-opacity="0"/>',
        '  </linearGradient>',
        '  <linearGradient id="line" x1="0" y1="0" x2="1" y2="0">',
        '    <stop offset="0%" stop-color="#1f6feb"/>',
        '    <stop offset="100%" stop-color="#58a6ff"/>',
        '  </linearGradient>',
        f'  <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">',
        '    <feGaussianBlur stdDeviation="3" result="b"/>',
        '    <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>',
        '  </filter>',
        '</defs>',
        f'<style><![CDATA[text{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif}} .axis{{font-size:11px;fill:#6e7681}} .axis-bold{{font-size:11px;fill:#8b949e;font-weight:600}} .value{{font-size:13px;fill:#58a6ff;font-weight:600}} .label{{font-size:10px;fill:#6e7681}}]]></style>',
        '<rect width="100%" height="100%" rx="12" fill="#0d1117"/>',
        f'<text x="{width-pad_r}" y="26" text-anchor="end" class="label">Stars · Updated {esc(dt.date.today().isoformat())}</text>',
    ]

    # horizontal grid + Y labels
    for tv in tick_vals:
        y = py(tv)
        lines.append(f'<line x1="{pad_l}" y1="{y:.2f}" x2="{width-pad_r}" y2="{y:.2f}" stroke="#21262d" stroke-width="1" stroke-dasharray="2,4"/>')
        lines.append(f'<text x="{pad_l-10}" y="{y+4:.2f}" text-anchor="end" class="axis">{int(round(tv))}</text>')

    # vertical ticks + X labels
    for idx in x_tick_idx:
        x = px(idx)
        label = x_dates[idx]
        lines.append(f'<line x1="{x:.2f}" y1="{pad_t}" x2="{x:.2f}" y2="{pad_t+plot_h}" stroke="#161b22" stroke-width="1"/>')
        lines.append(f'<text x="{x:.2f}" y="{pad_t+plot_h+20}" text-anchor="middle" class="axis">{esc(fmt_x(label))}</text>')

    # area fill + gradient line
    lines.append(f'<path d="{area_path}" fill="url(#area)"/>')
    lines.append(f'<polyline fill="none" stroke="url(#line)" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" points="{points}"/>')

    # endpoint dot + value badge
    ex, ey = px(len(y_values)-1), py(y_values[-1])
    lines.append(f'<circle cx="{ex:.2f}" cy="{ey:.2f}" r="4.5" fill="#58a6ff" filter="url(#glow)"/>')
    lines.append(f'<circle cx="{ex:.2f}" cy="{ey:.2f}" r="2.5" fill="#f0f6fc"/>')
    lines.append(f'<text x="{ex:.2f}" y="{ey-12:.2f}" text-anchor="middle" class="value">{y_values[-1]}</text>')
    lines.append('</svg>')
    return "\n".join(lines)


def main():
    owner = OWNER
    start_date = dt.date.fromisoformat(START_DATE_STR)
    if start_date > dt.date.today():
        print(
            f"Warning: STAR_CHART_START_DATE {start_date.isoformat()} is in the future, using today instead.",
            file=sys.stderr,
        )
        start_date = dt.date.today()

    try:
        star_dates, had_errors = fetch_star_dates(owner)
    except RateLimitError as exc:
        reset_str = ""
        if exc.reset_epoch:
            try:
                reset_str = f" (resets at {dt.datetime.utcfromtimestamp(exc.reset_epoch).isoformat()}Z)"
            except Exception:
                pass
        print(
            f"Warning: GitHub API rate limit reached{reset_str}. "
            f"Preserving the previous star chart instead of writing a zeroed-out one.",
            file=sys.stderr,
        )
        # Leave the existing SVG untouched so the workflow does not commit a flat-zero chart.
        sys.exit(0)
    except Exception as exc:
        print(f"Warning: failed to fetch stargazer history: {exc}", file=sys.stderr)
        star_dates, had_errors = [], True

    # If we couldn't fetch ANY data (auth failure, fully rate limited, etc.) but
    # did hit errors, keep the last good chart rather than silently resetting to 0.
    if had_errors and not star_dates:
        print(
            "Warning: fetched zero stargazer events due to errors. "
            "Preserving the previous star chart.",
            file=sys.stderr,
        )
        sys.exit(0)

    x_dates, y_values = build_series(star_dates, start_date)
    svg = render_svg(x_dates, y_values)

    out_path = OUTPUT
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(svg)

    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
