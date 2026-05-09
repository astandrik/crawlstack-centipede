#!/usr/bin/env python3
"""Build a Crawlstack contribution-grid animation for a GitHub profile README."""

from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


GRAPHQL_QUERY = """
query($userName: String!) {
  user(login: $userName) {
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks {
          firstDay
          contributionDays {
            date
            weekday
            contributionCount
            contributionLevel
            color
          }
        }
      }
    }
  }
}
"""

LIGHT_COLORS = {
    "NONE": "#ebedf0",
    "FIRST_QUARTILE": "#9be9a8",
    "SECOND_QUARTILE": "#40c463",
    "THIRD_QUARTILE": "#30a14e",
    "FOURTH_QUARTILE": "#216e39",
}

DARK_COLORS = {
    "NONE": "#161b22",
    "FIRST_QUARTILE": "#0e4429",
    "SECOND_QUARTILE": "#006d32",
    "THIRD_QUARTILE": "#26a641",
    "FOURTH_QUARTILE": "#39d353",
}


@dataclass(frozen=True)
class Day:
    date: str
    weekday: int
    week: int
    count: int
    level: str


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a PNG file")
    return struct.unpack(">II", data[16:24])


def collect_frames(directory: Path) -> list[Path]:
    frames = sorted(directory.glob("*.png"))
    if not frames:
        raise ValueError(f"No PNG frames found in {directory}")
    return frames


def data_uri(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def graphql_with_token(user_name: str, token: str) -> dict[str, Any]:
    payload = json.dumps(
        {"query": GRAPHQL_QUERY, "variables": {"userName": user_name}}
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "crawlstack-contribution-grid",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def graphql_with_gh(user_name: str) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={GRAPHQL_QUERY}",
            "-f",
            f"userName={user_name}",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(completed.stdout)


def load_calendar(user_name: str, fixture: Path | None) -> dict[str, Any]:
    if fixture:
        return json.loads(fixture.read_text(encoding="utf-8"))

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    payload = graphql_with_token(user_name, token) if token else graphql_with_gh(user_name)
    errors = payload.get("errors")
    if errors:
        raise RuntimeError(json.dumps(errors, indent=2))
    return payload


def extract_days(payload: dict[str, Any]) -> tuple[int, list[Day]]:
    calendar = payload["data"]["user"]["contributionsCollection"]["contributionCalendar"]
    days: list[Day] = []
    for week_index, week in enumerate(calendar["weeks"]):
        for day in week["contributionDays"]:
            days.append(
                Day(
                    date=day["date"],
                    weekday=int(day["weekday"]),
                    week=week_index,
                    count=int(day["contributionCount"]),
                    level=day.get("contributionLevel") or "NONE",
                )
            )
    return int(calendar["totalContributions"]), days


def nonlinear_contribution_path(days: list[Day]) -> list[Day]:
    active_days = [day for day in days if day.count > 0]
    if not active_days:
        return days

    unvisited = set(active_days)
    current = min(active_days, key=lambda day: (day.week, day.weekday))
    path = [current]
    unvisited.remove(current)

    while unvisited:
        candidates = list(unvisited)
        cross_row = [day for day in candidates if day.weekday != current.weekday]
        pool = cross_row if cross_row else candidates

        def score(day: Day) -> tuple[float, int, int, str]:
            week_distance = abs(day.week - current.week)
            day_distance = abs(day.weekday - current.weekday)
            direction_bias = 0 if (len(path) + day.weekday) % 2 == 0 else 1
            distance = week_distance * week_distance + day_distance * day_distance * 3.5
            return distance, direction_bias, day.week, day.date

        current = min(pool, key=score)
        path.append(current)
        unvisited.remove(current)

    return path


def frame_css(frame_count: int, frame_cycle: float) -> str:
    rules = [
        ".frame { opacity: 0; animation-duration: %.3fs; animation-timing-function: steps(1, end); animation-iteration-count: infinite; }"
        % frame_cycle
    ]
    step = 100.0 / frame_count
    for index in range(frame_count):
        start = index * step
        end = (index + 1) * step
        fallback_opacity = " opacity: 1;" if index == 0 else ""
        rules.append(
            f".frame-{index} {{{fallback_opacity} animation-name: show-frame-{index}; }}"
        )
        rules.append(
            "@keyframes show-frame-%d { 0%%, %.4f%% { opacity: 0; } %.4f%%, %.4f%% { opacity: 1; } %.4f%%, 100%% { opacity: 0; } }"
            % (index, max(start - 0.0001, 0.0), start, max(end - 0.0001, start), end)
        )
    return "\n      ".join(rules)


def pct(index: int, last_index: int) -> float:
    return 100.0 if last_index <= 0 else index * 100.0 / last_index


def cell_position(day: Day, grid_x: int, grid_y: int, step: int) -> tuple[float, float]:
    return grid_x + day.week * step, grid_y + day.weekday * step


def sprite_position(
    day: Day,
    grid_x: int,
    grid_y: int,
    step: int,
    cell_size: int,
    sprite_width: float,
    sprite_height: float,
) -> tuple[float, float]:
    x, y = cell_position(day, grid_x, grid_y, step)
    return x + cell_size / 2 - sprite_width / 2, y + cell_size / 2 - sprite_height / 2


def direction_for_segment(path: list[Day], index: int) -> str:
    if len(path) < 2:
        return "right"
    current = path[index]
    next_day = path[index + 1] if index + 1 < len(path) else path[index]
    if next_day.week < current.week:
        return "left"
    if next_day.week > current.week:
        return "right"
    if index > 0:
        return direction_for_segment(path, index - 1)
    return "right"


def path_keyframes(
    path: list[Day],
    grid_x: int,
    grid_y: int,
    step: int,
    cell_size: int,
    sprite_width: float,
    sprite_height: float,
) -> str:
    last = len(path) - 1
    rules = ["@keyframes crawl-path {"]
    for index, day in enumerate(path):
        x, y = sprite_position(day, grid_x, grid_y, step, cell_size, sprite_width, sprite_height)
        rules.append(f"  {pct(index, last):.4f}% {{ transform: translate({x:.2f}px, {y:.2f}px); }}")
    rules.append("}")
    return "\n      ".join(rules)


def direction_keyframes(path: list[Day], direction: str) -> str:
    last = len(path) - 1
    rules = [f"@keyframes crawl-{direction} {{"]
    for index, _day in enumerate(path):
        opacity = "1" if direction_for_segment(path, index) == direction else "0"
        rules.append(f"  {pct(index, last):.4f}% {{ opacity: {opacity}; }}")
    rules.append("}")
    return "\n      ".join(rules)


def eat_keyframes(path_index: int, path_last: int, duration: float) -> tuple[str, str]:
    eat_at = pct(path_index, path_last)
    settle_at = min(eat_at + max(0.25, 80.0 / max(duration, 1.0)), 100.0)
    name = f"eat-{path_index}"
    rule = (
        f".{name} {{ animation: {name} {duration:.3f}s linear infinite; transform-box: fill-box; transform-origin: center; }}\n"
        f"      @keyframes {name} {{\n"
        f"        0%, {eat_at:.4f}% {{ opacity: 1; transform: scale(1); }}\n"
        f"        {settle_at:.4f}%, 100% {{ opacity: 0; transform: scale(0.35); }}\n"
        f"      }}"
    )
    return name, rule


def cocoon_cell(
    *,
    x: float,
    y: float,
    size: int,
    fill: str,
    title: str,
    class_name: str | None,
    stroke: str,
    seam: str,
    highlight: str,
    active: bool,
) -> str:
    cx = size / 2
    cy = size / 2
    rx = size * 0.39
    ry = size * 0.47
    seam_x = size * 0.55
    seam_top = size * 0.22
    seam_bottom = size * 0.78
    highlight_cx = size * 0.38
    highlight_cy = size * 0.30
    highlight_rx = size * 0.12
    highlight_ry = size * 0.08
    stroke_width = 0.85 if active else 0.65
    seam_opacity = 0.42 if active else 0.30
    highlight_opacity = 0.45 if active else 0.22
    classes = "cocoon-cell" if class_name is None else f"cocoon-cell {class_name}"

    return f"""  <g transform="translate({x:.2f} {y:.2f})">
    <g class="{classes}">
      <title>{title}</title>
      <ellipse cx="{cx:.2f}" cy="{cy:.2f}" rx="{rx:.2f}" ry="{ry:.2f}" fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width:.2f}" transform="rotate(-16 {cx:.2f} {cy:.2f})" />
      <path d="M {seam_x:.2f} {seam_top:.2f} C {seam_x - 1.35:.2f} {size * 0.38:.2f}, {seam_x + 1.35:.2f} {size * 0.58:.2f}, {seam_x:.2f} {seam_bottom:.2f}" fill="none" stroke="{seam}" stroke-width="0.70" stroke-linecap="round" opacity="{seam_opacity:.2f}" />
      <ellipse cx="{highlight_cx:.2f}" cy="{highlight_cy:.2f}" rx="{highlight_rx:.2f}" ry="{highlight_ry:.2f}" fill="{highlight}" opacity="{highlight_opacity:.2f}" transform="rotate(-16 {highlight_cx:.2f} {highlight_cy:.2f})" />
    </g>
  </g>"""


def image_stack(frames: list[Path], width: float, height: float) -> str:
    lines = []
    for index, frame in enumerate(frames):
        lines.append(
            '    <image class="frame frame-%d" width="%.2f" height="%.2f" href="%s" />'
            % (index, width, height, data_uri(frame))
        )
    return "\n".join(lines)


def build_svg(
    *,
    payload: dict[str, Any],
    user_name: str,
    output: Path,
    theme: str,
    right_frames: list[Path],
    left_frames: list[Path],
    cell_size: int,
    gap: int,
    grid_x: int,
    grid_y: int,
    sprite_width: float,
    duration: float,
    frame_cycle: float,
) -> None:
    total_contributions, days = extract_days(payload)
    path = nonlinear_contribution_path(days)
    path_lookup = {day.date: index for index, day in enumerate(path)}
    max_week = max(day.week for day in days)
    step = cell_size + gap
    grid_width = max_week * step + cell_size
    grid_height = 6 * step + cell_size
    stage_width = grid_x + grid_width + 24

    natural_width, natural_height = png_size(right_frames[0])
    sprite_height = sprite_width * natural_height / natural_width
    stage_height = int(max(grid_y + grid_height + 30, grid_y + 6 * step + cell_size / 2 + sprite_height / 2 + 20))

    for frame in [*right_frames, *left_frames]:
        if png_size(frame) != (natural_width, natural_height):
            raise ValueError(f"{frame} does not match {natural_width}x{natural_height}")
    if len(right_frames) != len(left_frames):
        raise ValueError("running-right and running-left must have the same frame count")

    colors = DARK_COLORS if theme == "dark" else LIGHT_COLORS
    background = "#0d1117" if theme == "dark" else "#ffffff"
    grid_border = "#30363d" if theme == "dark" else "#d0d7de"
    empty_color = colors["NONE"]
    empty_seam = "#6e7681" if theme == "dark" else "#8c959f"
    active_stroke = "#8ff0a4" if theme == "dark" else "#1a7f37"
    active_seam = "#f0fff4" if theme == "dark" else "#096b2f"
    highlight = "#ffffff"
    base_cells: list[str] = []
    active_cells: list[str] = []
    eat_rules: list[str] = []
    path_last = len(path) - 1

    for day in days:
        x, y = cell_position(day, grid_x, grid_y, step)
        title = f"{day.date}: {day.count} contributions"
        base_cells.append(
            cocoon_cell(
                x=x,
                y=y,
                size=cell_size,
                fill=empty_color,
                title=title,
                class_name=None,
                stroke=grid_border,
                seam=empty_seam,
                highlight=highlight,
                active=False,
            )
        )
        if day.count <= 0:
            continue

        color = colors.get(day.level, day.level if day.level.startswith("#") else LIGHT_COLORS["FIRST_QUARTILE"])
        class_name, rule = eat_keyframes(path_lookup[day.date], path_last, duration)
        eat_rules.append(rule)
        active_cells.append(
            cocoon_cell(
                x=x,
                y=y,
                size=cell_size,
                fill=color,
                title=title,
                class_name=class_name,
                stroke=active_stroke,
                seam=active_seam,
                highlight=highlight,
                active=True,
            )
        )

    css = "\n      ".join(
        [
            "svg { background: transparent; }",
            f".stage {{ fill: {background}; }}",
            f".grid-outline {{ fill: none; stroke: {grid_border}; stroke-width: 1; opacity: 0.35; }}",
            ".cocoon-cell { transform-box: fill-box; transform-origin: center; }",
            "image { image-rendering: pixelated; }",
            ".runner { transform-box: fill-box; transform-origin: 0 0; animation: crawl-path %.3fs linear infinite; }"
            % duration,
            ".right-sprite { opacity: 1; animation: crawl-path %.3fs linear infinite, crawl-right %.3fs step-end infinite; }"
            % (duration, duration),
            ".left-sprite { opacity: 0; animation: crawl-path %.3fs linear infinite, crawl-left %.3fs step-end infinite; }"
            % (duration, duration),
            path_keyframes(path, grid_x, grid_y, step, cell_size, sprite_width, sprite_height),
            direction_keyframes(path, "right"),
            direction_keyframes(path, "left"),
            frame_css(len(right_frames), frame_cycle),
            *eat_rules,
        ]
    )

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{stage_width}" height="{stage_height}" viewBox="0 0 {stage_width} {stage_height}" role="img" aria-labelledby="title desc">
  <title id="title">Crawlstack eating {user_name}'s GitHub contributions</title>
  <desc id="desc">Crawlstack crawls across a GitHub contribution grid with {total_contributions} contributions and consumes active cells.</desc>
  <style>
    {css}
  </style>
  <rect class="stage" x="0" y="0" width="{stage_width}" height="{stage_height}" rx="8" />
  <rect class="grid-outline" x="{grid_x - 5}" y="{grid_y - 5}" width="{grid_width + 10}" height="{grid_height + 10}" rx="5" />
  <g class="base-cells">
{chr(10).join(base_cells)}
  </g>
  <g class="active-cells">
{chr(10).join(active_cells)}
  </g>
  <g class="runner right-sprite">
{image_stack(right_frames, sprite_width, sprite_height)}
  </g>
  <g class="runner left-sprite">
{image_stack(left_frames, sprite_width, sprite_height)}
  </g>
</svg>
"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(svg, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default=os.environ.get("GITHUB_REPOSITORY_OWNER", "astandrik"))
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--output", type=Path, default=Path("crawlstack/profile/github-contribution-grid-crawlstack.svg"))
    parser.add_argument("--theme", choices=["light", "dark"], default="light")
    parser.add_argument("--right-dir", type=Path, default=Path("crawlstack/frames/running-right"))
    parser.add_argument("--left-dir", type=Path, default=Path("crawlstack/frames/running-left"))
    parser.add_argument("--cell-size", type=int, default=13)
    parser.add_argument("--gap", type=int, default=4)
    parser.add_argument("--grid-x", type=int, default=24)
    parser.add_argument("--grid-y", type=int, default=36)
    parser.add_argument("--sprite-width", type=float, default=54.0)
    parser.add_argument("--duration", type=float, default=28.0)
    parser.add_argument("--frame-cycle", type=float, default=0.72)
    args = parser.parse_args()

    payload = load_calendar(args.user, args.fixture)
    build_svg(
        payload=payload,
        user_name=args.user,
        output=args.output,
        theme=args.theme,
        right_frames=collect_frames(args.right_dir),
        left_frames=collect_frames(args.left_dir),
        cell_size=args.cell_size,
        gap=args.gap,
        grid_x=args.grid_x,
        grid_y=args.grid_y,
        sprite_width=args.sprite_width,
        duration=args.duration,
        frame_cycle=args.frame_cycle,
    )


if __name__ == "__main__":
    main()
