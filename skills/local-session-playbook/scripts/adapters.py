"""Allowlisted event readers. Transcript bodies never become report records."""
from dataclasses import dataclass, field
from datetime import datetime
import re


@dataclass
class Turn:
    key: object
    start: float = None
    prompt: float = None
    end: float = None
    tier: str = "unknown"
    status: str = "incomplete"
    tools: set = field(default_factory=set)
    cues: set = field(default_factory=set)


@dataclass
class Session:
    key: str
    cwd: str
    child: bool = False
    turns: dict = field(default_factory=dict)

    def turn(self, key):
        return self.turns.setdefault(key, Turn(key))


def mapping(value):
    return value if isinstance(value, dict) else {}


def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 10**12 <= value < 10**14:
            value /= 1000
        return float(value) if 0 < value < 10**10 else None
    if isinstance(value, str):
        try:
            time = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return time.timestamp() if time.tzinfo else None
        except (ValueError, OverflowError):
            pass
    return None


def identifier(value):
    return value if isinstance(value, str) and value else None


def detect(event):
    kind = event.get("type")
    if kind == "session_meta" and isinstance(event.get("payload"), dict):
        return "codex"
    if kind == "session" and type(event.get("version")) is int and identifier(event.get("id")):
        return "dsh"
    if identifier(event.get("sessionId")) and kind in {"user", "assistant", "system", "summary", "progress"}:
        return "claude"
    return None


CUES = {
    "brainstorm": r"brainstorm|compare (?:the )?(?:options|approaches)|脑暴|头脑风暴|比较方案",
    "decision": r"wait for (?:my|the) (?:approval|answer)|before (?:you|we) (?:proceed|continue)|等我确认|等我决定|先别执行",
    "background": r"run in (?:the )?background|unattended|后台跑|无人值守|自动复验",
    "shared_resource": r"same (?:file|worktree|instance)|shared (?:file|instance)|同一(?:个)?(?:文件|工作树|实例)",
}
CUES = {name: re.compile(pattern, re.IGNORECASE) for name, pattern in CUES.items()}


def cue_labels(content):
    if isinstance(content, str):
        text = content[:4000]
    elif isinstance(content, list):
        text = " ".join(
            str(block.get("text", ""))[:1000] for block in content[:8]
            if isinstance(block, dict) and identifier(block.get("type")) in {"text", "input_text"}
        )[:4000]
    else:
        return set()
    return {name for name, pattern in CUES.items() if pattern.search(text)}


def user(turn, time, content, cues):
    if time is not None and (turn.prompt is None or time < turn.prompt):
        turn.prompt = time
    if cues:
        turn.cues.update(cue_labels(content))


def close(turn, end, status, diagnostics, reported_ms=None):
    if end is None or turn.start is None:
        return
    elapsed = end - turn.start
    if elapsed < 0 or (isinstance(reported_ms, (int, float)) and abs(elapsed - reported_ms / 1000) > 5):
        diagnostics["inconsistent_time"] += 1
        turn.status = "invalid"
    else:
        turn.end = end
        turn.status = status


CODEX_TOOLS = {"function_call", "custom_tool_call", "web_search_call", "local_shell_call", "image_generation_call"}
CODEX_ITEMS = {"CommandExecution", "FileChange", "McpToolCall", "DynamicToolCall", "WebSearch", "ImageGeneration"}


def codex(rows, diagnostics, cues):
    session = None
    current = None
    pending = None
    tier = "unknown"
    inherited_turns = set()
    for event in rows:
        kind = event.get("type")
        payload = mapping(event.get("payload"))
        subtype = identifier(payload.get("type"))
        time = timestamp(event.get("timestamp"))
        if kind == "session_meta":
            if session is not None:
                continue
            key = identifier(payload.get("id"))
            if key is None:
                return None
            child = payload.get("thread_source") not in (None, "user") or "subagent" in mapping(payload.get("source"))
            session = Session(key, identifier(payload.get("cwd")), child)
            continue
        if session is None:
            continue
        if kind == "event_msg" and subtype == "thread_settings_applied" and payload.get("thread_id") == session.key:
            raw_tier = mapping(payload.get("thread_settings")).get("service_tier")
            if isinstance(raw_tier, str):
                tier = {"default": "normal", "ultrafast": "ultrafast", "fast": "ultrafast"}.get(raw_tier, "unknown")
                if current:
                    current.tier = tier
        if kind == "event_msg" and subtype in {"task_started", "turn_started"}:
            key = identifier(payload.get("turn_id"))
            if key is None or time is None:
                diagnostics["missing_fields"] += 1
                current = None
                pending = None
                continue
            recorded = timestamp(payload.get("started_at"))
            if recorded is not None and abs(time - recorded) > 5:
                diagnostics["replayed_turns"] += 1
                inherited_turns.add(key)
                current = None
                pending = None
                continue
            current = session.turn(key)
            current.start, current.tier = time, tier
            if pending and 0 <= time - pending[0] <= 5:
                user(current, *pending, cues)
            pending = None
        elif kind == "event_msg" and subtype in {"task_complete", "turn_complete", "turn_aborted", "task_failed", "task_aborted"}:
            turn = session.turns.get(identifier(payload.get("turn_id")))
            if turn is not None:
                status = "completed" if subtype in {"task_complete", "turn_complete"} else "interrupted"
                close(turn, time, status, diagnostics, payload.get("duration_ms"))
            if current is turn:
                current = None
        elif kind == "event_msg" and subtype == "user_message":
            if current:
                user(current, time, payload.get("message"), cues)
            elif time is not None:
                pending = (time, payload.get("message"))
        elif kind == "event_msg" and subtype == "item_completed" and payload.get("thread_id") == session.key:
            item = mapping(payload.get("item"))
            turn_key = identifier(payload.get("turn_id"))
            if turn_key in inherited_turns:
                continue
            turn = session.turns.get(turn_key)
            if turn and item.get("type") == "UserMessage":
                user(turn, time, item.get("content"), cues)
            if turn and identifier(item.get("type")) in CODEX_ITEMS:
                tool_id = identifier(item.get("call_id")) or identifier(item.get("id"))
                turn.tools.add(("item", tool_id or len(turn.tools)))
        elif kind == "response_item" and current and subtype in CODEX_TOOLS:
            tool_id = identifier(payload.get("call_id")) or identifier(payload.get("id"))
            current.tools.add(("raw", tool_id or len(current.tools)))
    if session:
        for turn in session.turns.values():
            # Two representations may record the same calls. Prefer complete raw items.
            raw = {entry for entry in turn.tools if entry[0] == "raw"}
            items = turn.tools - raw
            turn.tools = max((raw, items), key=len)
    return session


def dsh(rows, diagnostics, cues):
    session = None
    current = None
    pending = None
    for event in rows:
        kind = event.get("type")
        data = mapping(event.get("data"))
        time = timestamp(event.get("time"))
        if kind == "session" and session is None:
            key = identifier(event.get("id"))
            if key is None:
                return None
            if event.get("version") not in (0, 1, 2, 3, 4):
                diagnostics["unsupported_version"] += 1
                return None
            depth = event.get("delegationDepth")
            session = Session(key, identifier(event.get("cwd")), type(depth) is int and depth > 0)
        elif session and kind == "turn/start" and type(data.get("turn")) is int:
            current = session.turn(data["turn"])
            current.start = time
            if pending and time is not None and 0 <= time - pending[0] <= 5:
                user(current, *pending, cues)
            pending = None
        elif session and kind == "turn/end" and type(data.get("turn")) is int:
            turn = session.turns.get(data["turn"])
            if turn:
                reason = mapping(data.get("reason")).get("kind", data.get("reason"))
                status = "completed" if reason == "completed" else "interrupted"
                close(turn, time, status, diagnostics)
            if current is turn:
                current = None
        elif session and kind == "user/message" and data.get("role", "user") == "user":
            if current:
                user(current, time, data.get("content"), cues)
            elif time is not None:
                pending = (time, data.get("content"))
        elif session and kind == "tool/call" and type(data.get("turn")) is int:
            turn = session.turns.get(data["turn"])
            if turn:
                turn.tools.add(identifier(data.get("callId")) or len(turn.tools))
    return session


def claude(rows, diagnostics, cues):
    session = None
    current = None
    for event in rows:
        key = identifier(event.get("sessionId"))
        if key is None or event.get("type") not in {"user", "assistant"}:
            continue
        if session is None:
            session = Session(key, identifier(event.get("cwd")), event.get("isSidechain") is True)
        if key != session.key or (event.get("isSidechain") is True and not session.child):
            continue
        session.cwd = session.cwd or identifier(event.get("cwd"))
        message = mapping(event.get("message"))
        content = message.get("content")
        blocks = [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
        time = timestamp(event.get("timestamp"))
        if event["type"] == "user":
            text_input = isinstance(content, str) or any(block.get("type") == "text" for block in blocks)
            if not text_input or any(block.get("type") == "tool_result" for block in blocks):
                continue
            if event.get("isMeta") or event.get("isCompactSummary"):
                continue
            turn_key = identifier(event.get("uuid")) or time
            if turn_key is None or time is None:
                diagnostics["missing_fields"] += 1
                current = None
                continue
            current = session.turn(turn_key)
            current.start = time
            user(current, time, content, cues)
        elif current:
            for block in blocks:
                if identifier(block.get("type")) in {"tool_use", "server_tool_use"}:
                    current.tools.add(identifier(block.get("id")) or len(current.tools))
            if message.get("stop_reason") in ("end_turn", "stop_sequence"):
                close(current, time, "completed", diagnostics)
            elif message.get("stop_reason") in ("max_tokens", "refusal"):
                close(current, time, "interrupted", diagnostics)
            elif any(block.get("type") == "tool_use" for block in blocks):
                current.end, current.status = None, "incomplete"
    return session


ADAPTERS = {"codex": codex, "dsh": dsh, "claude": claude}
