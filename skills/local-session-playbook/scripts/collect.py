#!/usr/bin/env python3
"""Summarize local session logs without exporting transcript text or paths."""
import argparse
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from itertools import chain
import json
import os
from pathlib import Path
import re
from statistics import median, median_low
import subprocess
import sys

from adapters import ADAPTERS, detect, mapping, timestamp
from decision import WAIT_BINS, clean_buckets

DSH_NAME = re.compile(r"session(?:\.v([1-9][0-9]*))?\.jsonl(?:\.(?:zstd|zst))?\Z")
MAX_LINE = 8 * 1024 * 1024


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def files_under(root, diagnostics):
    if root.is_symlink():
        diagnostics["symlink_skipped"] += 1
    elif root.is_file():
        yield root
    elif root.is_dir():
        def onerror(_error):
            diagnostics["read_error"] += 1

        for folder, _, names in os.walk(root, onerror=onerror):
            for name in sorted(names):
                path = Path(folder, name)
                if path.is_symlink():
                    diagnostics["symlink_skipped"] += 1
                elif name.endswith((".jsonl", ".jsonl.zstd", ".jsonl.zst")) and path.is_file():
                    yield path
    else:
        diagnostics["missing_source"] += 1


def discovery(inputs, diagnostics):
    seen = set()
    grouped = defaultdict(list)
    for expected, root in inputs:
        for path in files_under(root, diagnostics):
            canonical = path.resolve()
            if canonical in seen:
                diagnostics["duplicate_paths"] += 1
                continue
            seen.add(canonical)
            match = DSH_NAME.fullmatch(path.name)
            if match:
                generation = int(match.group(1) or 0)
                grouped[canonical.parent].append((generation, path, expected))
            else:
                yield expected, path
    for variants in grouped.values():
        # A compressed and uncompressed copy may coexist. Prefer plain at the same generation.
        _, path, expected = max(variants, key=lambda value: (value[0], value[1].suffix == ".jsonl"))
        diagnostics["dsh_superseded_files"] += len(variants) - 1
        yield expected, path


@contextmanager
def open_stream(path):
    if path.suffix not in {".zstd", ".zst"}:
        with path.open("rb") as stream:
            yield stream
        return
    with subprocess.Popen(["zstd", "-dc", "--", str(path)], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as proc:
        class CompressedStream:
            def readline(self, size):
                line = proc.stdout.readline(size)
                if not line and proc.wait():
                    raise OSError("could not decompress a session")
                return line

        try:
            yield CompressedStream()
        finally:
            proc.stdout.close()
            if proc.poll() is None:
                proc.terminate()


def rows(stream, diagnostics):
    for line in iter(lambda: stream.readline(MAX_LINE + 1), b""):
        if len(line) > MAX_LINE:
            diagnostics["oversized_records"] += 1
            while line and not line.endswith(b"\n"):
                line = stream.readline(MAX_LINE + 1)
            continue
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            diagnostics["malformed_records"] += 1
            continue
        if isinstance(event, dict) and isinstance(event.get("type"), str):
            yield event
        else:
            diagnostics["malformed_records"] += 1


def project_match(cwd, project):
    if project is None:
        return True
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        return False
    try:
        return Path(cwd).resolve().is_relative_to(project)
    except (OSError, ValueError, RuntimeError):
        return False


def read_source(expected, path, project, diagnostics, cues):
    with open_stream(path) as stream:
        events = rows(stream, diagnostics)
        for event in events:
            kind = detect(event)
            if kind is not None:
                break
        else:
            diagnostics["unknown_format"] += 1
            return None, None
        if expected and kind != expected:
            diagnostics["unexpected_format"] += 1
            return None, None
        if kind in {"codex", "dsh"}:
            header = mapping(event.get("payload")) if kind == "codex" else event
            if not project_match(header.get("cwd"), project):
                diagnostics["project_unknown" if not header.get("cwd") else "outside_project"] += 1
                return None, None
        session = ADAPTERS[kind](chain((event,), events), diagnostics, cues)
    if session is None:
        diagnostics["unusable_session"] += 1
        return None, None
    if not project_match(session.cwd, project):
        diagnostics["project_unknown" if not session.cwd else "outside_project"] += 1
        return None, None
    if session.child:
        diagnostics["child_sessions"] += 1
        return None, None
    return kind, session


def wait_buckets():
    return {key: {"rounds": 0, "other_chat_prompt": 0} for key, _, _, _ in WAIT_BINS}


def median_other_chats(prompt_rows, completed):
    if not prompt_rows:
        return None
    events = sorted(
        event
        for session, turn in completed if turn.start < turn.end
        for event in ((turn.start, 1, session), (turn.end, -1, session))
    )
    active = Counter()
    counts = []
    cursor = 0
    for at, session in sorted(prompt_rows):
        while cursor < len(events) and events[cursor][0] <= at:
            _, direction, other = events[cursor]
            active[other] += direction
            if not active[other]:
                del active[other]
            cursor += 1
        counts.append(len(active) - (session in active))
    return median_low(counts)


def summary(rows):
    prompts = [(t.prompt, s) for s, t in rows if t.prompt is not None]
    completed = [(s, t) for s, t in rows if t.status == "completed" and t.end is not None]
    all_times = sorted(time for time, _ in prompts)
    session_times = defaultdict(list)
    for time, session in sorted(prompts):
        session_times[session].append(time)
    buckets = wait_buckets()
    normal_buckets = wait_buckets()
    normal_prompts = []
    eligible_prompts = []
    latency = defaultdict(list)
    for session, turn in completed:
        duration = turn.end - turn.start
        tier = turn.tier
        latency[tier].append(duration)
        if turn.prompt is None or turn.prompt >= turn.end:
            continue
        bucket = next(key for key, _, end, _ in WAIT_BINS if end is None or duration < end)
        own = session_times[session]
        all_count = bisect_left(all_times, turn.end) - bisect_right(all_times, turn.prompt)
        own_count = bisect_left(own, turn.end) - bisect_right(own, turn.prompt)
        switched = all_count > own_count
        buckets[bucket]["rounds"] += 1
        buckets[bucket]["other_chat_prompt"] += switched
        eligible_prompts.append((turn.prompt, session))
        if tier == "normal":
            normal_buckets[bucket]["rounds"] += 1
            normal_buckets[bucket]["other_chat_prompt"] += switched
            normal_prompts.append((turn.prompt, session))
    return {
        "sessions": len({s for s, _ in rows}),
        "rounds": len(rows),
        "completed": len(completed),
        "source_range_utc": [iso(min(all_times)), iso(max(all_times))] if all_times else None,
        "same_harness_overlap": buckets,
        "decision": {
            "cohort": "normal" if normal_prompts else "all_completed",
            "buckets": normal_buckets if normal_prompts else buckets,
            "median_other_sessions": median_other_chats(normal_prompts or eligible_prompts, completed),
        },
        "duration_seconds_by_tier": {
            tier: {"rounds": len(values), "median": round(median(values), 2)}
            for tier, values in sorted(latency.items())
        },
        "rounds_with_tool_signal": sum(bool(t.tools) for _, t in rows),
        "cues": dict(sorted(Counter(cue for _, t in rows for cue in t.cues).items())),
    }


def classic_pattern(rows_by_harness):
    candidates = []
    for harness, rows in rows_by_harness.items():
        completed = [(s, t) for s, t in rows if t.status == "completed"]
        by_end = sorted(completed, key=lambda item: item[1].end)
        by_start = sorted(completed, key=lambda item: item[1].start)
        ends = [t.end for _, t in by_end]
        starts = [t.start for _, t in by_start]
        for session, turn in completed:
            at = turn.prompt
            if at is None:
                continue
            recent = {s: t.end for s, t in by_end[bisect_left(ends, at - 120):bisect_left(ends, at)]
                      if s != session}
            if len(recent) != 3:
                continue
            running = {s: t.end for s, t in by_start[:bisect_right(starts, at)]
                       if s != session and t.end > at}
            if len(running) != 4 or recent.keys() & running.keys():
                continue
            candidates.append((at, harness, recent, running))
    if not candidates:
        return None
    at, harness, recent, running = max(candidates, key=lambda x: x[0])
    return {"harness": harness, "sent_at_utc": iso(at),
            "recently_finished_offsets_seconds": sorted(round(end - at, 1) for end in recent.values()),
            "still_running_end_offsets_seconds": sorted(round(end - at, 1) for end in running.values())}


def number(value):
    if type(value) is int and 0 <= value <= 10**12:
        return value
    if type(value) is float and 0 <= value <= 10**12:
        return round(value, 3)
    return None


def postgres_summary(path):
    with path.open("rb") as stream:
        data = stream.read(1_000_001)
    if len(data) > 1_000_000:
        raise ValueError("PostgreSQL summary too large")
    source = mapping(json.loads(data))
    prompts = mapping(source.get("prompts"))
    overlap = mapping(source.get("overlap"))
    if not isinstance(source.get("inventory"), dict) or not prompts:
        raise ValueError("unsupported PostgreSQL summary")
    return {
        "scope": "source report scope; project and date filters were not reapplied",
        "all_turns": number(source.get("turns")),
        "first_user_turn_utc": iso(timestamp(source.get("first_user_turn"))),
        "most_recent_user_turn_utc": iso(timestamp(source.get("most_recent_user_turn"))),
        "prompts": {k: number(prompts.get(k)) for k in ("prompts", "with_overlap", "median_others")},
        "decision_buckets": clean_buckets(source.get("decision_buckets")),
        "overlap": {k: number(overlap[k]) for k in ("peak", "wall_seconds", "summed_seconds", "overlapping_seconds") if k in overlap},
    }


def defaults():
    home = Path.home()
    codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex").expanduser()
    dsh_home = Path(os.environ.get("DSH_HOME") or home / ".dsh").expanduser()
    claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude").expanduser()
    return [("codex", codex_home / "sessions"), ("codex", codex_home / "archived_sessions"),
            ("dsh", dsh_home / "sessions"), ("claude", claude_home / "projects")]


def args_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--project", type=Path, help="workspace path (default: current directory)")
    scope.add_argument("--all-projects", action="store_true", help="explicit opt-in to all projects")
    parser.add_argument("--input", type=Path, action="append", default=[],
                        help="probe only these explicit files or directories (repeatable)")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--since", help="explicit UTC start, ISO datetime with offset")
    parser.add_argument("--until", help="exclusive end, ISO datetime with offset; default now")
    parser.add_argument("--cues", action="store_true", help="count allowlisted cues; never prints excerpts")
    parser.add_argument("--postgres-report", type=Path, help="optional existing codex_pg.py report --json")
    parser.add_argument("--postgres-only", action="store_true", help="ingest only the supplied aggregate")
    return parser


def preference(turn):
    return turn.status == "completed", turn.prompt is not None, len(turn.tools)


def main():
    parser = args_parser()
    options = parser.parse_args()
    now = datetime.now(timezone.utc)
    end = timestamp(options.until) if options.until else now.timestamp()
    if options.days <= 0 or options.days > 36500 or end is None:
        parser.error("use --days 1..36500 and a valid --until")
    start = timestamp(options.since) if options.since else (datetime.fromtimestamp(end, timezone.utc) - timedelta(days=options.days)).timestamp()
    if start is None or start >= end:
        parser.error("use a valid --since before --until")
    if options.postgres_only and (not options.postgres_report or options.input):
        parser.error("--postgres-only needs --postgres-report and no --input")
    project = None if options.all_projects else (options.project or Path.cwd()).expanduser().resolve()
    inputs = [(None, p.expanduser()) for p in options.input] if options.input else defaults()
    diagnostics = Counter()
    sessions = {}
    if not options.postgres_only:
        for expected, path in discovery(inputs, diagnostics):
            diagnostics["files_probed"] += 1
            try:
                harness, session = read_source(expected, path, project, diagnostics, options.cues)
            except FileNotFoundError:
                diagnostics["zstd_unavailable" if path.exists() and path.suffix in {".zstd", ".zst"} else "read_error"] += 1
                continue
            except (OSError, RuntimeError):
                diagnostics["read_error"] += 1
                continue
            if session is None:
                continue
            key = harness, session.key
            if key in sessions:
                diagnostics["duplicate_sessions"] += 1
                previous = sessions[key]
                for turn_id, turn in session.turns.items():
                    prior = previous.turns.get(turn_id)
                    if prior is not None:
                        diagnostics["duplicate_turns"] += 1
                    if prior is None or preference(turn) > preference(prior):
                        previous.turns[turn_id] = turn
            else:
                sessions[key] = session
    grouped = defaultdict(list)
    seen_turns = set()
    for (harness, key), session in sessions.items():
        for turn in session.turns.values():
            if turn.start is None or turn.prompt is None:
                diagnostics["missing_start_or_user_prompt"] += 1
                continue
            if not start <= turn.prompt < end:
                diagnostics["outside_date_range"] += 1
                continue
            if turn.status == "invalid":
                continue
            if turn.end is not None and turn.end >= end:
                turn.end, turn.status = None, "incomplete"
            if turn.end is not None and turn.prompt > turn.end:
                diagnostics["inconsistent_time"] += 1
                continue
            boundary = (harness, None if harness == "codex" else key, turn.key)
            if boundary in seen_turns:
                diagnostics["duplicate_turns"] += 1
                continue
            seen_turns.add(boundary)
            grouped[harness].append((key, turn))
    report = {
        "schema_version": 1,
        "scope": {"project": "all" if project is None else "selected", "start_utc": iso(start), "until_utc": iso(end)},
        "harnesses": {harness: summary(grouped[harness]) for harness in ADAPTERS},
        "diagnostics": dict(sorted(diagnostics.items())),
        "a8_pattern": classic_pattern(grouped),
        "limits": ["duration is wall clock, not model thinking time",
                   "other chat prompts are observed activity, not user idle time",
                   "tool counts are signals, not proof of resource conflicts",
                   "unmatched tier samples do not establish speedup"],
    }
    if options.postgres_report:
        try:
            report["postgres"] = postgres_summary(options.postgres_report.expanduser())
        except (OSError, ValueError, OverflowError, RecursionError):
            parser.error("could not read a supported local PostgreSQL aggregate JSON")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
