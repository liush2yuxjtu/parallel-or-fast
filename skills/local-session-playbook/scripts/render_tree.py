#!/usr/bin/env python3
"""Generate a standalone Chinese decision tree from an aggregate report."""
import argparse
from html import escape
import json
from pathlib import Path

from decision import WAIT_BINS, clean_buckets, decision_cutoffs


HARNESSES = {"codex": "Codex", "dsh": "DSH", "claude": "Claude Code"}


def decision_evidence(report, harness):
    sources = []
    for key, label in HARNESSES.items():
        if harness and key != harness:
            continue
        source = (report.get("harnesses") or {}).get(key) or {}
        decision = source.get("decision") or {}
        buckets = clean_buckets(decision.get("buckets") or source.get("same_harness_overlap"))
        sample = sum(entry["rounds"] for entry in buckets.values())
        if sample:
            tier = " 普通档" if decision.get("cohort") == "normal" else ""
            sources.append((sample, label + tier, buckets, decision.get("median_other_sessions")))
    if sources:
        return max(sources, key=lambda item: item[0])

    if harness in (None, "codex"):
        postgres = report.get("postgres") or {}
        buckets = clean_buckets(postgres.get("decision_buckets"))
        sample = sum(entry["rounds"] for entry in buckets.values())
        if sample:
            median = (postgres.get("prompts") or {}).get("median_others")
            return sample, "PostgreSQL 普通档", buckets, median
    return 0, "", {}, None


def evidence(report, synthetic, sample, source, harness):
    if sample:
        prefix = "合成样本 · " if synthetic else ""
        return f"{prefix}{source} · {sample} 轮"
    if harness:
        prefix = "合成样本 · " if synthetic else ""
        return f"{prefix}{HARNESSES[harness]} · 缺分段样本"
    total = sum(s.get("rounds", 0) for s in report.get("harnesses", {}).values())
    pg = report.get("postgres") or {}
    if synthetic:
        return f"合成样本 · {total} 轮"
    if total:
        scope = report.get("scope") or {}
        label = "全部项目" if scope.get("project") == "all" else "指定项目"
        start = escape(str(scope.get("start_utc", ""))[:10])
        end = escape(str(scope.get("until_utc", ""))[:10])
        return f"{label} · {start}–{end} · {total} 轮"
    prompt_count = (pg.get("prompts") or {}).get("prompts")
    if prompt_count is not None:
        return f"本机 PostgreSQL · 普通档 {int(prompt_count)} 轮"
    if pg.get("all_turns") is not None:
        return f"本机 PostgreSQL · {int(pg['all_turns'])} 轮"
    return "规则示意 · 缺本地样本"


def offsets(values, length):
    return (isinstance(values, list) and len(values) == length and
            all(type(v) in (int, float) and abs(v) < 10**9 for v in values))


def timeline(report, synthetic):
    pattern = report.get("a8_pattern") or {}
    finished = pattern.get("recently_finished_offsets_seconds")
    running = pattern.get("still_running_end_offsets_seconds")
    observed = (offsets(finished, 3) and offsets(running, 4)
                and all(-120 <= value < 0 for value in finished)
                and all(value > 0 for value in running))
    if not observed:
        finished, running = [-100, -48, -12], [80, 122, 160, 190]
    finished, running = iter(finished), list(running)
    longest = max(running) or 1
    running = iter(running)
    center, left, right = 170, 38, 338
    bars = []
    for index, name in enumerate(("A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8")):
        y = 23 + index * 22
        if name in ("A1", "A3", "A5"):
            extent = center + (center - left - 8) * next(finished) / 120
            bar_start, cls = left, "finished"
        elif name != "A8":
            extent = center + (right - center) * next(running) / longest
            bar_start, cls = left + 12, "running"
        else:
            bar_start, extent, cls = center, right, "sent"
        bars.append(f'<text x="9" y="{y + 4}">{name}</text><path class="{cls}" d="M{bar_start} {y}H{extent:.1f}"/>')
    caption = "合成" if synthetic else "实测" if observed else "示意"
    return f"""<figure aria-label="发出 A8 时，A1、A3、A5 已结束，A2、A4、A6、A7 继续运行。时间线只显示结束和发出的相对位置。">
<svg viewBox="0 0 352 198" aria-hidden="true">
<path class="marker" d="M{center} 6V190"/>
{"".join(bars)}
</svg>
<figcaption>{caption} · <span class="done">■ 3 完成</span> <span class="busy">■ 4 在跑</span> <span class="now">■ 发出</span></figcaption>
</figure>"""


def duration(seconds):
    if seconds < 60:
        return f"{seconds} 秒"
    return f"{seconds // 60} 分"


def parallel_leaf(short, long, median):
    start = f"<small>先试 {median + 1} 条</small>" if type(median) is int and median > 0 else ""
    parallel = f'<strong class="answer a">A · 普通档并行</strong>{start}'
    if short is None and long in (None, 0):
        return parallel
    options = []
    if short is not None:
        options.append((f"<{duration(short)}", '<strong class="answer neutral">留单聊</strong>'))
    if long is None:
        options.append(("其余", f'<strong class="answer a">A 可试</strong>{start}'))
    else:
        if short is None or short < long:
            middle = ("<" if short is None else f"{duration(short)}–<") + duration(long)
            options.append((middle, '<strong class="answer neutral">酌情</strong>'))
        options.append((f"≥{duration(long)}", parallel))
    items = "".join(f'<li><span class="branch wait">{escape(label)}</span>{answer}</li>'
                    for label, answer in options)
    return f'<div class="node">预计等待？</div><ul>{items}</ul>'


def wait_chart(buckets, short, long):
    rows = []
    boundary_keys = {key for key, start, end, _ in WAIT_BINS
                     if (short is not None and end == short) or start == long}
    for key, start, end, label in WAIT_BINS:
        if boundary_keys and key not in boundary_keys:
            continue
        bucket = buckets.get(key) or {}
        if not bucket.get("rounds"):
            continue
        total, switched = bucket["rounds"], bucket["other_chat_prompt"]
        share = 100 * switched / total
        color = "short" if short and end and end <= short else "long" if long is not None and start >= long else ""
        rows.append(
            f'<div class="bar-row {color}"><span>{escape(label)}</span>'
            f'<span class="bar" aria-hidden="true"><i style="width:{share:.2f}%"></i></span>'
            f'<span><b>{share:.0f}%</b> <small>{switched}/{total}</small></span></div>'
        )
    if not rows:
        return ""
    note = ""
    if short is None and long is None:
        note = "<small>未见分界</small>" if any(b["rounds"] >= 20 for b in buckets.values()) else "<small>样本不足</small>"
    return f'<aside class="chart" aria-label="历史中等待回复时发消息给别的会话"><h3>等待时另发消息</h3>{"".join(rows)}{note}</aside>'


def page(report, synthetic=False, harness=None):
    sample, source, buckets, median = decision_evidence(report, harness)
    strip = evidence(report, synthetic, sample, source, harness)
    short, long = decision_cutoffs(buckets)
    parallel = parallel_leaf(short, long, median if sample >= 20 else None)
    chart = wait_chart(buckets, short, long)
    graph = timeline(report, synthetic)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; base-uri 'none'; form-action 'none'; img-src data:; font-src data:; media-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>多会话并行还是 Ultrafast</title>
<style>
:root {{ color-scheme: light dark; --bg:#eeeae0; --paper:#fffdf8; --ink:#17231d; --muted:#515a51; --line:#c7c8bb; --a:#146149; --b:#8c351f; --n:#785815; --running:#176579; }}
* {{ box-sizing:border-box }}
@media (prefers-color-scheme:dark) {{
  :root {{ --bg:#141a17; --paper:#202924; --ink:#f0f1e7; --muted:#c5ccc1; --line:#526157; --a:#73dcaf; --b:#ffac91; --n:#f5cd7b; --running:#73ccdc; }}
}}
body {{ background:var(--bg); color:var(--ink); margin:0; font:16px/1.45 -apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans CJK SC",sans-serif }}
main {{ width:min(100% - 28px, 850px); margin:0 auto; padding:clamp(24px,5vw,62px) 0 34px }}
header {{ border-top:4px solid var(--ink); padding:16px 1px 0; margin-bottom:24px }}
h1 {{ margin:0; font-size:clamp(29px,5vw,58px); letter-spacing:-.05em; line-height:1.14 }}
small,figcaption {{ color:var(--muted) }}
header small {{ display:block; margin-top:11px }}
section {{ background:var(--paper); border:1px solid var(--line); border-radius:15px; margin:0 0 14px; padding:clamp(14px,3.4vw,29px) }}
h2 {{ margin:0 0 14px; font-size:clamp(26px,3.8vw,37px); letter-spacing:-.04em }}
h2 span {{ font-family:ui-monospace,monospace; color:var(--a); margin-right:.32em }}
.pick-layout {{ display:grid; gap:18px; align-items:start }}
@media (min-width:740px) {{
  .pick-layout:has(.chart) {{ grid-template-columns:minmax(0,1fr) minmax(250px,.66fr); gap:18px }}
  .chart {{ align-self:center }}
}}
.tree {{ font-size:clamp(13px,2.4vw,17px); font-weight:700 }}
.tree ul {{ list-style:none; padding:0 0 0 clamp(8px,2.5vw,21px); margin:0 0 0 .42em; border-left:2px solid var(--line) }}
.tree li {{ position:relative; margin:0; padding:5px 0 0 }}
.tree li:has(ul) {{ padding-bottom:6px }}
.tree li::before {{ content:""; width:clamp(8px,2.5vw,21px); border-top:2px solid var(--line); position:absolute; left:calc(-1 * clamp(8px,2.5vw,21px)); top:1.12em }}
.node {{ display:inline-block; border:1px solid var(--line); padding:.3em .58em; border-radius:6px; margin-bottom:3px }}
.branch {{ display:inline-block; min-width:1.8em; color:var(--muted); font-size:.85em }}
.branch.wait {{ margin-right:.48em; font-variant-numeric:tabular-nums; white-space:nowrap }}
.answer {{ display:inline-block; margin:0 0 3px; border-radius:6px; border:1px solid currentColor; padding:.3em .55em }}
.answer + small {{ display:inline-block; margin:0 0 .35em .44em; font-size:.78em; white-space:nowrap }}
.a,.done {{ color:var(--a) }} .b {{ color:var(--b) }} .neutral,.now {{ color:var(--n) }} .busy {{ color:var(--running) }}
.chart {{ margin:0; padding:12px; border:1px solid var(--line); border-radius:9px }}
.chart h3 {{ margin:0 0 8px; font-size:16px }}
.bar-row {{ display:grid; align-items:center; grid-template-columns:5.7em minmax(16px,1fr) auto; column-gap:.55em; margin:.34em 0; font-size:14px; font-variant-numeric:tabular-nums }}
.bar-row > span:first-child {{ white-space:nowrap }}
.bar-row small {{ font-size:.8em }}
.bar {{ background:var(--bg); border-radius:3px; height:8px }}
.bar i {{ height:8px; border-radius:3px; display:block; background:var(--running) }}
.bar-row.short i {{ background:var(--n) }}
.bar-row.long i {{ background:var(--a) }}
figure {{ margin:0; text-align:center }}
svg {{ display:block; width:min(100%,610px); height:auto; margin:auto }}
svg text {{ fill:var(--ink); stroke:none; font:11px ui-monospace,monospace }}
svg path {{ fill:none; stroke-linecap:round; stroke-width:6 }}
svg .finished {{ stroke:var(--a) }}
svg .running {{ stroke:var(--running) }}
svg .sent {{ stroke:var(--n) }}
svg .marker {{ stroke:var(--line); stroke-width:2; stroke-dasharray:3 4 }}
figcaption {{ font-size:14px; margin-top:4px }}
figcaption span {{ white-space:nowrap; margin-left:.35em }}
:focus-visible {{ outline:2px solid var(--b); outline-offset:3px }}
</style>
</head>
<body>
<main>
<header><h1>并行还是 Ultrafast？</h1><small>{strip}</small></header>
<section aria-labelledby="pick">
<h2 id="pick"><span>01</span>怎么选</h2>
<div class="pick-layout">
<div class="tree">
<div class="node">下一步等回复？</div>
<ul>
<li><span class="branch">是</span><strong class="answer b">B · Ultrafast 单会话</strong></li>
<li><span class="branch">否</span><div class="node" aria-label="有至少两项可以独立进行的任务吗？">≥2 件独立？</div>
<ul>
<li><span class="branch">否</span><strong class="answer neutral">单聊后台</strong></li>
<li><span class="branch">是</span><div class="node" aria-label="争同一个工具实例或文件？">争同一资源？</div>
<ul>
<li><span class="branch">是</span><strong class="answer neutral">冲突步骤排队</strong></li>
<li><span class="branch">否</span>{parallel}</li>
</ul></li></ul></li></ul>
</div>{chart}</div></section>
<section aria-labelledby="a8"><h2 id="a8"><span>02</span>A8 发出时</h2>{graph}</section>
</main>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--harness", choices=HARNESSES, help="use the selected harness for wait-time guidance")
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    args.output.write_text(page(report, args.synthetic, args.harness), encoding="utf-8")


if __name__ == "__main__":
    main()
