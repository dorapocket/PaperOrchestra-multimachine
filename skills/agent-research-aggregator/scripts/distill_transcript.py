#!/usr/bin/env python3
"""
distill_transcript.py — compress Claude Code conversation transcripts.

Claude Code stores the full conversation for every session as newline-delimited
JSON at:

    ~/.claude/projects/<encoded-project-path>/<session-uuid>.jsonl

These transcripts are the richest record of what an experiment actually did, but
they are enormous and mostly mechanical. Measured composition of a real
research project (4 sessions, 51 MB of content):

    tool_result (file reads, command stdout) .... 60%   <- mechanical, no novelty
    tool_use payloads (write/edit bodies, diffs)  19%   <- mechanical, no novelty
    meta / snapshot lines ....................... 17%   <- harness bookkeeping
    assistant text (methodology, narration) ....  2.6%  <- THE SIGNAL
    user text (prompts, ideas) .................  1.2%  <- THE SIGNAL

So this distiller is **content-first**, not budget-first:

  KEEP IN FULL   user prompts, assistant narration/methodology text, and
                 assistant reasoning (thinking). These carry the ideas and the
                 method — they are never truncated by default.
  KEEP COMPACT   a one-line trace of each tool action (tool name + command /
                 file path), so the "what was done" skeleton survives — but the
                 heavy payload is dropped.
  KEEP (recaps)  system-written recap summaries (subtype away_summary) — these
                 state the session goal/method in a sentence. ~8 KB total.
  KEEP (results) the DELIVERABLE results — a subagent's synthesized report
                 (Agent/Task tool_result) and workflow output (which arrives as
                 <task-notification> user messages) are kept in full even though
                 ordinary tool results are dropped.
  DROP           tool_result bodies (file dumps, command output), file-write
                 contents, edit diffs, image/base64 blobs, AND the bulky
                 bookkeeping meta: file-history snapshots, tool-schema
                 attachments, mode/permission/hook/queue/duration markers.
  REDACT         API keys, tokens, bearer headers, AWS keys.

Result: ~3.8% of raw size with **zero methodology lost** (a 143 MB project
distills to ~125 KB). Nothing that affects the paper's novelty is removed.

Knobs (all optional):
  --max-chars N        per-session cap (0 = unlimited, the default). If set and
                       exceeded, head + tail are kept and the iterative middle
                       is elided — prose blocks are still never cut mid-block.
  --max-block-chars N  safety cap on a single content block (0 = unlimited).
  --no-tools           drop the compact tool trace entirely (prose only).
  --keep-results N     keep the first N chars of each tool result (0 = drop).
  --no-thinking        drop assistant reasoning blocks.

Importable:  distill_session(path, ...) -> str (markdown)
             session_meta(path) -> dict
CLI:         distill one file, or every *.jsonl under a directory.

Usage:
    python distill_transcript.py --in session.jsonl
    python distill_transcript.py --in ~/.claude/projects \\
        --out-dir workspace/ara/_transcripts
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Defaults (content-first: 0 == unlimited / keep in full)
# ---------------------------------------------------------------------------

DEFAULT_MAX_CHARS = 0         # per-session cap; 0 = no truncation
DEFAULT_MAX_BLOCK = 0         # per content-block cap; 0 = full
DEFAULT_KEEP_RESULTS = 0      # chars of each tool_result to keep; 0 = drop body
TOOL_ARG_CHARS = 240          # truncate long tool-arg values in the trace line

# Harness/slash-command noise that is not research content.
_NOISE_USER = re.compile(
    r"^\s*<(local-command-stdout|command-name|command-message|command-args)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Secret redaction (defence in depth — the extraction prompt also strips PII)
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"(?i)(?:api[_-]?key|secret|token|password|passwd|authorization|bearer)"
               r"\s*[:=]\s*['\"]?[A-Za-z0-9._/+-]{12,}['\"]?"),
]


def _redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


# Markers of high-signal lines that must survive bounded truncation: synthesized
# subagent/workflow results, recap summaries, and workflow task-notifications.
_PIN_MARKERS = (" result**:", "📋 **recap**", "<task-notification>")


def _is_pinned(line: str) -> bool:
    return any(mk in line for mk in _PIN_MARKERS)


def _clip(text, limit):
    """Redact + strip. limit==0 means keep in full (no truncation)."""
    text = _redact(str(text).strip())
    if limit and len(text) > limit:
        return text[:limit].rstrip() + f" …[+{len(text) - limit} chars]"
    return text


# ---------------------------------------------------------------------------
# Tool-call compaction: keep WHAT was done, drop the payload bulk
# ---------------------------------------------------------------------------

# For these tools the file path is the signal; the content/diff is bulk.
_PATH_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "NotebookRead"}
_SEARCH_TOOLS = {"Grep", "Glob"}

# Tools whose tool_result IS the deliverable (a subagent's synthesized report, a
# workflow's output). Their results are kept in full even when ordinary tool
# results are dropped. Note: a Workflow launch returns only an ack; the real
# workflow output arrives later as a <task-notification> user message (kept as
# plain user text). TaskOutput fetches a finished background task's output.
_RESULT_KEEP_TOOLS = {"Agent", "Task", "Workflow", "TaskOutput"}


def _compact_tool_use(block: dict) -> str:
    """One short line describing a tool call. Never includes file contents,
    edit diffs, or other heavy payloads."""
    name = block.get("name", "tool")
    inp = block.get("input", {}) or {}
    if not isinstance(inp, dict):
        return f"{name}: {_clip(inp, TOOL_ARG_CHARS)}"

    if name == "Bash":
        return f"Bash: `{_clip(inp.get('command', ''), TOOL_ARG_CHARS)}`"
    if name in _PATH_TOOLS:
        # path only — the written content / read result is dropped
        return f"{name}: {inp.get('file_path') or inp.get('notebook_path') or ''}"
    if name in _SEARCH_TOOLS:
        pat = inp.get("pattern", "")
        loc = inp.get("path", "") or inp.get("glob", "")
        return f"{name}: {pat}" + (f" in {loc}" if loc else "")
    if name == "Task" or name.endswith("Agent"):
        return f"{name}: {_clip(inp.get('description') or inp.get('prompt', ''), TOOL_ARG_CHARS)}"

    # Generic: keep arg names + truncated string values (drop big blobs).
    parts = []
    for k, v in inp.items():
        if isinstance(v, str):
            v = _clip(v, TOOL_ARG_CHARS)
        elif isinstance(v, (list, dict)):
            v = f"<{type(v).__name__}>"
        parts.append(f"{k}={v}")
    return f"{name}: " + ", ".join(parts)


def _text_from_result_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    chunks.append(b.get("text", ""))
                elif b.get("type") == "image":
                    chunks.append("[image omitted]")
            else:
                chunks.append(str(b))
        return "\n".join(chunks)
    return str(content)


# ---------------------------------------------------------------------------
# Per-line rendering
# ---------------------------------------------------------------------------

def _render_system_meta(obj, max_block):
    """Most non-message lines are bookkeeping (file-history snapshots,
    tool-schema attachments, mode/permission/hook/queue markers) and carry no
    research value. The exception is system-written *recap summaries*
    (subtype away_summary / *summary), which state the session goal/method in a
    sentence — keep those."""
    if "attachment" in obj:                       # tool-schema deltas, file refs
        return []
    sub = obj.get("subtype") or ""
    content = obj.get("content")
    if not isinstance(content, str) or not content.strip():
        return []
    if sub == "away_summary" or "summary" in sub:
        return [f"- 📋 **recap**: {_clip(content, max_block)}"]
    return []


def _render_event(obj, *, keep_thinking, max_block, drop_tools, keep_results,
                  keep_meta, id2name):
    """Return zero or more distilled markdown lines for one transcript event.

    `id2name` maps tool_use_id -> tool name (built as we go) so a tool_result
    can be attributed to the tool that produced it — results from *synthesis*
    tools (subagents / workflows) ARE the deliverable and are always kept, even
    when ordinary tool results are dropped."""
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return _render_system_meta(obj, max_block) if keep_meta else []
    if obj.get("isMeta"):  # injected meta user messages (caveats, summaries)
        return []

    role = msg.get("role")
    sidechain = " (subagent)" if obj.get("isSidechain") else ""
    content = msg.get("content")
    lines = []

    if role == "user":
        if isinstance(content, str):
            if _NOISE_USER.match(content):       # slash-command / stdout wrappers
                return []
            # NB: <task-notification> messages (workflow/background-task results)
            # arrive here as plain user strings and are kept in full.
            txt = _clip(content, max_block)
            if txt:
                lines.append(f"- 👤 **user**{sidechain}: {txt}")
        elif isinstance(content, list):
            # user-role lists are tool_result blocks. Mechanical results (file
            # reads, command output) are dropped unless --keep-results; but
            # results from subagents/workflows are the synthesized output and
            # are always kept in full.
            for b in content:
                if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                    continue
                name = id2name.get(b.get("tool_use_id"), "")
                if name in _RESULT_KEEP_TOOLS:
                    res = _clip(_text_from_result_content(b.get("content", "")), max_block)
                    if res:
                        lines.append(f"    ↳ **{name} result**: {res}")
                elif keep_results:
                    res = _clip(_text_from_result_content(b.get("content", "")), keep_results)
                    if res:
                        lines.append(f"    ↳ result: {res}")
        return lines

    if role == "assistant" and isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":                       # methodology / narration — FULL
                txt = _clip(b.get("text", ""), max_block)
                if txt:
                    lines.append(f"- 🤖 **assistant**{sidechain}: {txt}")
            elif t == "thinking" and keep_thinking:  # reasoning — FULL
                txt = _clip(b.get("thinking", ""), max_block)
                if txt:
                    lines.append(f"    💭 (thinking) {txt}")
            elif t == "tool_use":
                if b.get("id"):                   # remember for result attribution
                    id2name[b["id"]] = b.get("name")
                if not drop_tools:                # compact skeleton only
                    lines.append(f"    ⚙ {_compact_tool_use(b)}")
        return lines

    return lines


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def session_meta(path: Path) -> dict:
    """Cheap metadata pass: session id, project cwd, turn count, time span."""
    sid = path.stem
    cwd = git_branch = None
    first_ts = last_ts = None
    n_user = n_assistant = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = cwd or o.get("cwd")
                git_branch = git_branch or o.get("gitBranch")
                ts = o.get("timestamp")
                if ts:
                    first_ts = first_ts or ts
                    last_ts = ts
                m = o.get("message")
                if isinstance(m, dict):
                    if m.get("role") == "user" and not o.get("isMeta"):
                        n_user += 1
                    elif m.get("role") == "assistant":
                        n_assistant += 1
    except OSError:
        pass
    return {
        "session_id": sid, "cwd": cwd, "git_branch": git_branch,
        "first_ts": first_ts, "last_ts": last_ts,
        "user_turns": n_user, "assistant_turns": n_assistant,
    }


def distill_session(path, max_chars=DEFAULT_MAX_CHARS, keep_thinking=True,
                    max_block_chars=DEFAULT_MAX_BLOCK,
                    keep_results=DEFAULT_KEEP_RESULTS, drop_tools=False,
                    keep_meta=True) -> str:
    """Distill one .jsonl transcript into compact, content-first markdown.

    By default (max_chars=0) nothing is truncated — every user prompt and
    assistant/thinking block is kept in full; only mechanical tool I/O is
    dropped. Set max_chars>0 to bound very long sessions (head+tail kept).
    """
    path = Path(path)
    meta = session_meta(path)
    header = (
        f"# Session {meta['session_id']}\n"
        f"- project (cwd): {meta.get('cwd') or 'unknown'}"
        f"  | branch: {meta.get('git_branch') or '-'}\n"
        f"- span: {meta.get('first_ts') or '?'} → {meta.get('last_ts') or '?'}"
        f"  | turns: {meta['user_turns']} user / {meta['assistant_turns']} assistant\n\n"
    )

    all_lines = []
    id2name = {}     # tool_use_id -> tool name, for result attribution
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                all_lines.extend(_render_event(
                    obj, keep_thinking=keep_thinking, max_block=max_block_chars,
                    drop_tools=drop_tools, keep_results=keep_results,
                    keep_meta=keep_meta, id2name=id2name))
    except OSError as e:
        all_lines.append(f"- [error reading transcript: {e}]")

    # No per-session cap → keep everything (the default).
    if not max_chars:
        return header + "\n".join(all_lines) + "\n"

    budget = max(0, max_chars - len(header))

    def _b(s):
        return len(s) + 1

    if sum(_b(x) for x in all_lines) <= budget:
        return header + "\n".join(all_lines) + "\n"

    # Bounded. High-signal lines are PINNED and always kept even if they sit in
    # the elided middle: synthesized subagent/workflow results, recap summaries,
    # and workflow <task-notification>s. The remaining budget is filled with a
    # head + tail of the ordinary turns, and everything is emitted in original
    # order with elision markers.
    pinned = {i for i, ln in enumerate(all_lines) if _is_pinned(ln)}
    keep = set()
    pin_cost = sum(_b(all_lines[i]) for i in pinned)
    if pin_cost <= budget:
        keep |= pinned
        remaining = budget - pin_cost
        rest = [i for i in range(len(all_lines)) if i not in pinned]
        head_budget, used = int(remaining * 0.55), 0
        for i in rest:
            if used + _b(all_lines[i]) > head_budget:
                break
            keep.add(i); used += _b(all_lines[i])
        used2 = 0
        for i in reversed(rest):
            if i in keep:
                continue
            if used2 + _b(all_lines[i]) > remaining - used:
                break
            keep.add(i); used2 += _b(all_lines[i])
    else:
        # Pinned content alone exceeds the budget — keep pinned head+tail.
        order = sorted(pinned)
        used = 0
        for i in order:
            if used + _b(all_lines[i]) > int(budget * 0.55):
                break
            keep.add(i); used += _b(all_lines[i])
        used2 = 0
        for i in reversed(order):
            if i in keep:
                continue
            if used2 + _b(all_lines[i]) > budget - used:
                break
            keep.add(i); used2 += _b(all_lines[i])

    out, prev = [], None
    for i, ln in enumerate(all_lines):
        if i not in keep:
            continue
        if prev is not None and i > prev + 1:
            out.append(f"\n_…{i - prev - 1} lower-signal turns elided…_\n")
        out.append(ln)
        prev = i
    note = (f"\n\n_(session exceeds {max_chars} chars; synthesized results, "
            f"recaps & workflow outputs pinned; ordinary turns head+tail-sampled)_")
    return header + "\n".join(out) + note + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")


def split_markdown(md: str, max_bytes: int) -> list[str]:
    """Split distilled markdown into <= max_bytes chunks at line boundaries,
    repeating the session header on each part. Used so a long session stays
    within the pipeline's per-file size budget without dropping any content.
    max_bytes <= 0 disables splitting."""
    if max_bytes <= 0 or len(md.encode("utf-8")) <= max_bytes:
        return [md]
    lines = md.split("\n")
    h_end = 0
    for i, l in enumerate(lines):
        if i > 0 and l.strip() == "":
            h_end = i
            break
    header = lines[:h_end]
    hdr_bytes = len("\n".join(header).encode("utf-8")) + 1
    chunks, cur, size = [], [], hdr_bytes
    for l in lines[h_end:]:
        b = len(l.encode("utf-8")) + 1
        if cur and size + b > max_bytes:
            chunks.append(header + cur)
            cur, size = [], hdr_bytes
        cur.append(l)
        size += b
    if cur:
        chunks.append(header + cur)
    n = len(chunks)
    out = []
    for k, c in enumerate(chunks, 1):
        c = list(c)
        if c:
            c[0] = c[0] + f"  (part {k}/{n})"
        out.append("\n".join(c))
    return out


def main():
    ap = argparse.ArgumentParser(description="Distill Claude Code .jsonl transcripts (content-first)")
    ap.add_argument("--in", dest="inp", required=True,
                    help="A .jsonl file, or a directory scanned recursively for *.jsonl")
    ap.add_argument("--out-dir", default=None,
                    help="Write one <project>__<session>.md per transcript here. "
                         "If omitted with a single file input, prints to stdout.")
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                    help="Per-session cap (0 = unlimited, default). If set, head+tail kept.")
    ap.add_argument("--max-block-chars", type=int, default=DEFAULT_MAX_BLOCK,
                    help="Cap a single content block (0 = full, default)")
    ap.add_argument("--keep-results", type=int, default=DEFAULT_KEEP_RESULTS,
                    help="Keep first N chars of each tool result (0 = drop, default)")
    ap.add_argument("--no-tools", action="store_true",
                    help="Drop the compact tool trace entirely (prose only)")
    ap.add_argument("--no-thinking", action="store_true",
                    help="Drop assistant reasoning (thinking) blocks")
    ap.add_argument("--no-meta", action="store_true",
                    help="Drop system recap summaries (away_summary etc.)")
    ap.add_argument("--since", default=None,
                    help="ISO 8601 date; only distill sessions modified after this")
    args = ap.parse_args()

    src = Path(args.inp).expanduser()
    since_dt = (datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
                if args.since else None)

    if src.is_file():
        files = [src]
    elif src.is_dir():
        files = sorted(src.rglob("*.jsonl"))
    else:
        print(f"[ERROR] not found: {src}", file=sys.stderr)
        sys.exit(1)

    if since_dt:
        files = [f for f in files
                 if datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc) >= since_dt]
    if not files:
        print("[WARN] no transcripts matched.", file=sys.stderr)
        sys.exit(0)

    kw = dict(max_chars=args.max_chars, keep_thinking=not args.no_thinking,
              max_block_chars=args.max_block_chars, keep_results=args.keep_results,
              drop_tools=args.no_tools, keep_meta=not args.no_meta)

    if args.out_dir:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        total_in = total_out = 0
        for f in files:
            md = distill_session(f, **kw)
            proj = _safe_name(f.parent.name)
            (out_dir / f"{proj}__{_safe_name(f.stem)}.md").write_text(md, encoding="utf-8")
            total_in += f.stat().st_size
            total_out += len(md.encode("utf-8"))
        ratio = (total_out / total_in) if total_in else 0
        print(f"Distilled {len(files)} session(s): "
              f"{total_in/1048576:.1f} MB → {total_out/1024:.1f} KB "
              f"({ratio*100:.2f}% of original) into {out_dir}")
    else:
        if len(files) > 1:
            print(f"[ERROR] {len(files)} transcripts found; --out-dir required for "
                  f"directory input.", file=sys.stderr)
            sys.exit(1)
        sys.stdout.write(distill_session(files[0], **kw))


if __name__ == "__main__":
    main()
