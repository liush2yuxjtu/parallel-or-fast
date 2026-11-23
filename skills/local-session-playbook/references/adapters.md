# Input and output contract

Run from a checkout or an installed skill. Scripts sit beside this directory in `../scripts`. Run help for current flags.

| Source | Discovery | Trusted event boundaries |
| --- | --- | --- |
| Codex | `${CODEX_HOME:-~/.codex}/sessions` and `archived_sessions` | Session header, task/turn events, user item, tool records. Cross-file turn IDs and re-stamped events are checked. |
| DSH | `${DSH_HOME:-~/.dsh}/sessions` | Session header and turn events, with observed v4 and compatibility readers for earlier generations sharing that shape. The latest canonical file wins per directory. `.zst` and `.zstd` require a local `zstd` executable. |
| Claude Code | `${CLAUDE_CONFIG_DIR:-~/.claude}/projects` | User prompts, assistant message blocks and terminal stop reasons. Tool results and sidechains do not become user prompts. Timing comes from transcript timestamps. Fixture verified until a real Claude installation is checked. |
| Other | Explicit `--input FILE_OR_DIRECTORY` | Detect a supported format from structured events. Unknown formats report omissions. Add a reader only after obtaining safe schema evidence. |
| PostgreSQL | `--postgres-report` on existing `codex_pg.py report --json` aggregate; optionally `--postgres-only` | Read an allowlist of numeric aggregates and report timestamps. Dates and project scope remain those of the originating SQL report. |

JSON report schema v1 has `scope`, `harnesses`, `diagnostics`, `a8_pattern`, `limits` and optional `postgres`. Each harness contains rounds, completions, first/last observed user prompt timestamps, duration median and count per recorded tier, counts of rounds with tool signals, and four wall-clock buckets for same-harness other-chat sends. The numerator counts an observed send strictly after the target prompt and before the target reply. A range with zero eligible rounds has no percentage. Activity through other applications is outside this metric.

## Personalized wait numbers

`harnesses.<name>.decision` supplies the eligible cohort, its `buckets`, and `median_other_sessions`. Each bucket records `rounds` and `other_chat_prompt`. Use known ordinary-tier turns when available and all completed turns otherwise. The tree defaults to the harness with the largest eligible cohort in the selected project and date range. To choose a different cohort, pass `--harness codex`, `dsh`, or `claude` to the renderer. If no matching raw cohort is present, a Codex PostgreSQL report can supply the same four counters through `decision_buckets` and its median through `prompts.median_others`. Older exports still show their scope without a numerical cutoff.

The wait bands are under 30 s, 30 s to under 2 min, 2 min to under 5 min, and 5 min or more. For each band with at least 20 eligible turns, use a Wilson rate interval with multiplier 1.96 as a stability guard. Extend the stay-single band from the shortest wait while its upper bound is at most 25%. Start the try-parallel band at the earliest supported band for which all supported longer bands have a lower bound at least 60%. Leave the in-between range open. The highlighted fractions are the actual counts in the bands at the two boundaries. The prospective thresholds are heuristics for this workload. They do not estimate speedup or measure the cost of switching. If seen, the suggested starting chat count is one plus the historical median number of other chats already active at prompt time. It is a starting point.

The A8 detector seeks an observed send within two minutes of three distinct finished chats while four different chats with verifiable completion ran past A8's send. It may miss active rounds without reliable closing timestamps. Use a labeled illustration when `a8_pattern` is empty.

Each reader takes a stream of parsed events and returns an internal session and turns. New readers should specify what establishes a prompt, start, end, child session, tool signal and service tier. Preserve unknown values. Add fixtures for duplicated and interrupted records before labeling a source supported. The report contains no raw text, machine paths, tool names, arguments, or session IDs. Project/time bounds and aggregates can still be private.

Primary format references: [Codex rollout event guide](https://github.com/openai/codex/blob/main/codex-rs/ext/memories/templates/memories/read_path.md), [DSH storage architecture](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md), [Claude Code configuration and history location](https://code.claude.com/docs/en/settings).
