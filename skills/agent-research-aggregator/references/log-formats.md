# Agent Log Formats

Reference for `discover_logs.py`. Describes what each agent type stores and
which files are most likely to contain experiment data.

---

## Claude Code (`.claude/`)

Claude Code stores all persistent state under `.claude/` at the project root
(or `~/.claude/` for global state).

### Memory files — HIGH VALUE

```
.claude/projects/<workspace-hash>/memory/
    *.md          # Structured memory entries (frontmatter: name, description, type)
~/.claude/projects/<workspace-hash>/memory/
    *.md          # Same, global location
```

Memory files use this frontmatter schema:
```yaml
---
name: <title>
description: <one-line hook>
type: user | feedback | project | reference
---
```

Types to prioritize:
- `type: project` — contains experiment goals, decisions, blockers
- `type: feedback` — contains "what worked / what didn't" patterns
- `type: user` — background context (role, domain knowledge)
- `type: reference` — external links + dataset/codebase pointers

### CLAUDE.md — HIGH VALUE

```
CLAUDE.md                   # Project-level instructions
.claude/CLAUDE.md           # Alternative location
```

Often contains: project description, experimental context, constraints,
design decisions that inform the research framing.

### Task outputs — MEDIUM VALUE

Claude Code task outputs (from the `TaskOutput` tool) may appear as:
```
.claude/task-outputs/
    *.md
    *.txt
```

These contain agent responses to long-running tasks — may include benchmark
runs, code generation results, test outputs.

### Todos — LOW VALUE (structure only)

```
.claude/todos/
    *.json        # {id, content, status, priority}
```

Useful for understanding what experiments were planned vs. completed.

### Conversation transcripts — HIGHEST VALUE, but NOT collected by `discover_logs.py`

```
~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl   # full session transcript
~/.claude/projects/<encoded-cwd>/subagents/agent-*.jsonl # subagent (sidechain) sessions
```

This is the complete, ground-truth record of every experiment session: each
line is one event (`{type, message:{role, content[...]}, cwd, gitBranch,
timestamp, isSidechain, ...}`), with content blocks of type `text`,
`thinking`, `tool_use`, and `tool_result`.

**Why `discover_logs.py` skips them:** they are enormous and noisy — a single
research project routinely totals 30–150 MB once tool outputs and file dumps are
included, far past the 200 KB per-file cap, and reading them raw would blow the
LLM context window. Note also that transcripts always live in the **global**
`~/.claude/projects/` (keyed by the encoded working directory), **not** in a
project-local `.claude/`.

**Composition (measured, 4 real research sessions = 51 MB of content):**

| part | share | value |
|---|---|---|
| `tool_result` (file reads, command stdout) | 60% | mechanical |
| `tool_use` payloads (write/edit bodies, diffs) | 19% | mechanical |
| meta: file-history snapshots + tool-schema attachments + state markers | 17% | bookkeeping |
| **assistant text** (methodology / narration) | 2.6% | **the signal** |
| **user text** (prompts / ideas) | 1.2% | **the signal** |
| `thinking` | ~0% | (empty in stored transcripts) |

So distillation is **content-first**, not budget-first — Phase 0
(`collect_machine.py` / `distill_transcript.py`):
- **keep in full** (never truncated): user prompts, assistant
  methodology/narration text, assistant reasoning (thinking);
- **keep**: system **recap summaries** (`subtype: away_summary`) — they state
  the session goal/method in one sentence (high value, ~8 KB);
- **keep compact**: a one-line trace per tool call (command / file path);
- **drop**: tool_result bodies, file-write contents, edit diffs, image/base64
  blobs, AND the bulky meta (file-history snapshots, tool-schema attachments,
  `mode`/`permission`/`hook`/`queue`/`turn_duration`/`ai-title` markers);
- **redact**: API keys, tokens, bearer headers, AWS keys.

Result ≈ 2–4% of raw size with **zero methodology lost**. Long sessions are
split into ≤150 KB part-files (no content dropped) so each fits the extraction
budget; `--max-chars N` optionally bounds a session (head+tail kept).

Standalone distiller (useful for a single session or ad-hoc inspection):
```bash
python skills/agent-research-aggregator/scripts/distill_transcript.py \
    --in ~/.claude/projects/<encoded-cwd> \
    --out-dir workspace/ara/_transcripts --max-chars 30000
```

Subagent transcripts (`agent-*.jsonl`, under `subagents/`) are usually redundant
with the main session and are excluded by default — opt in only if a subagent
held work the main thread never summarized.

---

## Cursor (`.cursor/`)

Cursor stores workspace AI data under `.cursor/` at the project root.

### Chat history — HIGH VALUE

```
.cursor/chat/
    chatHistory.json        # Array of {role, content, timestamp} objects
    *.chat                  # Per-session chat files (same format)
```

Also check SQLite databases:
```
~/.cursor/User/globalStorage/
    *.db                    # SQLite; table `ItemTable` has key-value chat data
```

SQLite query: `SELECT value FROM ItemTable WHERE key LIKE '%chat%'`

### Rules — MEDIUM VALUE

```
.cursor/rules/
    *.md                    # Cursor rules (may describe project + constraints)
.cursorrules                # Root-level rules file
```

### Notes / scratchpad — MEDIUM VALUE

```
.cursor/notes/
    *.md
```

---

## Antigravity (`.antigravity/`)

Antigravity is a multi-worker coding agent. Stores per-task logs and
worker outputs.

### Worker logs — HIGH VALUE

```
.antigravity/workers/
    <worker-id>/
        log.jsonl           # Newline-delimited JSON events
        output.md           # Final worker output
        task.json           # Task specification
```

Each `log.jsonl` line:
```json
{"ts": "ISO-8601", "type": "tool_result|message|error", "content": "..."}
```

### Task registry — MEDIUM VALUE

```
.antigravity/tasks/
    <task-id>.json          # {id, description, status, created_at, outputs[]}
.antigravity/task-registry.json   # Index of all tasks
```

### Workspace snapshots — LOW VALUE (size risk)

```
.antigravity/snapshots/
    <snapshot-id>/          # Git-bundle or diff snapshots between runs
```

Skip these unless `--include-snapshots` is passed (not default).

---

## OpenClaw (`.openclaw/`)

OpenClaw follows a similar structure to Claude Code but uses different
file names.

### Session logs — HIGH VALUE

```
.openclaw/sessions/
    <session-id>/
        conversation.md     # Full conversation in markdown
        artifacts/
            *.py, *.json    # Generated code + data files
```

### Memory — HIGH VALUE

```
.openclaw/memory/
    *.md                    # Structured notes (same frontmatter as Claude Code)
```

### Run outputs — MEDIUM VALUE

```
.openclaw/runs/
    <run-id>/
        stdout.log
        stderr.log
        exit_code.txt
        metrics.json        # Agent-emitted key-value metrics
```

---

## General project files (scanned regardless of agent)

These are scanned in the project root and common subdirectory names regardless
of which agent produced them:

| Pattern | Priority | Rationale |
|---|---|---|
| `results*.{json,csv,tsv}` | HIGH | Likely benchmark output |
| `experiments*.{json,yaml}` | HIGH | Experiment configs + results |
| `*.ipynb` | HIGH | Jupyter notebooks with outputs |
| `run_*.log`, `train_*.log` | HIGH | Training/eval logs |
| `metrics.json`, `eval.json` | HIGH | Structured metric files |
| `ablation*.{md,json}` | HIGH | Ablation study data |
| `README.md` (root only) | MEDIUM | Often summarizes experiments |
| `notes*.md`, `NOTES.md` | MEDIUM | Researcher notes |
| `config*.{yaml,json,toml}` | MEDIUM | Hyperparameter configs |
| `*.log` (root level) | LOW | Generic logs; scan headers only |

**Skip always:**
- `node_modules/`, `.git/`, `__pycache__/`, `*.pyc`
- Files > 200 KB (note path in report but don't read)
- Binary files (check magic bytes: `\x00` in first 512 bytes)
- Credential-like files: `*.pem`, `*.key`, `.env`, `credentials*`

---

## Extraction priority ranking

When logs exceed the batch size budget, process in this order:

1. Memory files (`.claude/memory/`, `.openclaw/memory/`)
2. Chat history / conversation logs with tool outputs
3. `metrics.json`, `eval.json`, structured result files
4. Jupyter notebooks (`.ipynb`)
5. Training logs (`run_*.log`, `train_*.log`)
6. CLAUDE.md / `.cursorrules` / project notes
7. Task specifications and todos
8. Generic README / notes files
