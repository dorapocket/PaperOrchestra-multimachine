---
name: agent-research-aggregator
description: Pre-pipeline aggregator that scans AI agent cache directories (.claude, .cursor, .antigravity, .openclaw) or any user-specified directory for experimentation logs, extracts insights and numeric results, and formats them as PaperOrchestra-ready inputs (idea.md + experimental_log.md). TRIGGER when the user says "aggregate my agent logs for paper writing", "extract experiments from my coding agent history", "prepare PaperOrchestra inputs from my cache", "turn my agent logs into a paper", mentions a folder or directory they want to use as the basis for a paper, or wants to run PaperOrchestra but only has scattered agent experiment histories rather than structured inputs. Run this BEFORE paper-orchestra. Also called automatically by paper-orchestra when workspace/inputs/idea.md or workspace/inputs/experimental_log.md are missing.
---

# agent-research-aggregator

---

## Should I run? (decision gate)

Before starting Phase 1, check whether aggregation is actually needed:

| Situation | Action |
|---|---|
| `workspace/inputs/idea.md` **and** `workspace/inputs/experimental_log.md` both exist and are non-empty | **Skip this skill entirely.** Proceed directly to `paper-orchestra`. |
| Either file is missing or empty, **and** the user provided a directory path | **Run this skill** with that directory as `--search-roots`. |
| Either file is missing or empty, **and** no directory was provided | Scan cwd and `~` by default; show the discovery summary to the user before continuing. |
| The inputs exist but look thin (e.g. idea.md has < 5 lines, no numeric data in experimental_log.md) | **Ask the user** whether to supplement with aggregation or proceed as-is. |

The skill is intentionally a pre-pass — it is cheap to skip and should only run when the structured inputs don't already exist.

**Was the work spread across several machines, or do you want the actual
conversation history (not just memory files)?** Then run **Phase 0** first to
collect + merge per-machine transcripts before Phase 1. See below.

---

## Phase 0 — Multi-machine collection & conversation transcripts (optional)

`discover_logs.py` (Phase 1) only catalogs Claude Code **memory** files,
`CLAUDE.md`, todos, and general result files. It deliberately does **not** read
the conversation transcripts at `~/.claude/projects/<encoded-path>/*.jsonl` —
those are the richest record of what an experiment did, but they are huge
(commonly 30–150 MB **per project**) and would blow the context window if read
raw. Phase 1 also only scans the **local** machine.

Use Phase 0 when **either** is true:
- experiments ran through Claude Code on **more than one machine**, or
- you want the **conversation history itself** folded into extraction, not just
  whatever happened to be written to memory.

Phase 0 has two steps. It produces the same `discovered_logs.json` that Phase 1
would, so Phases 2–4 run unchanged afterward.

### Phase 0a — collect on EACH machine (`collect_machine.py`)

Run on every machine that has relevant history. It distills each transcript
(`distill_transcript.py`), copies memory/result files, tags everything with the
machine's host id, and writes a small portable **bundle**.

```bash
python skills/agent-research-aggregator/scripts/collect_machine.py \
    --out ./po-bundle \
    --search-roots ~/my-project \
    --since 2026-01-01 \
    --tar              # -> po-bundle-<host>-<date>.tar.gz, small enough to scp
```

**Distillation is content-first.** Measured on real transcripts, ~95% of the
bytes are mechanical (tool_result file/command dumps 60%, write/edit payloads
19%, file-history snapshots + tool-schema attachments + state markers 17%) and
only ~4% is the actual signal (user prompts + assistant methodology text). So
the distiller:
- **keeps in full** every user prompt, assistant narration/methodology block,
  and reasoning (thinking) — **never truncated by default**, so a long method
  write-up survives intact;
- **keeps** system **recap summaries** (`away_summary`) — one-sentence
  statements of the session goal/method;
- **keeps a one-line trace** of each tool call (command / file path), payload
  dropped;
- **drops** tool results, file-write contents, edit diffs, snapshots,
  tool-schema dumps, and harness state lines;
- **redacts** API keys / tokens.

Result: ~2–4% of raw size with **no methodology lost** (143 MB project →
~3 MB, or ~250 KB if you bound it). Long sessions are split into ≤150 KB
part-files so each stays within the extraction budget — still nothing dropped.

Defaults worth knowing:
- **Transcripts included by default** (distilled). `--no-transcripts` =
  memory/result-only (old behaviour).
- **Subagent (sidechain) transcripts excluded by default** — mostly redundant
  with the main session. `--include-subagents` keeps them (10–50× more files).
- **No content truncation by default** (`--max-chars 0`). Set `--max-chars
  60000` to bound very long sessions (head+tail kept) for far fewer extraction
  batches at some completeness cost.
- Leaner variants: `--no-tools` (prose only), `--no-meta` (drop recaps),
  `--keep-results N` (re-include N chars of tool output), `--chunk-bytes`,
  `--max-block-chars`. `--project <substr>` narrows to one project.

Then move the bundles to one central machine by **any** transport you like
(`scp`, `rsync`, a shared drive). The tool prints an example `scp` line.

### Phase 0b — merge on the CENTRAL machine (`merge_bundles.py`)

```bash
# first pass: list merged projects (exits 2 — choose one, same as Phase 1.5)
python skills/agent-research-aggregator/scripts/merge_bundles.py \
    --bundles /inbox/po-bundle-gpu1.tar.gz /inbox/po-bundle-gpu2 \
    --by-basename \
    --out workspace/ara/discovered_logs.json

# second pass: filter to the chosen project (exits 0)
python skills/agent-research-aggregator/scripts/merge_bundles.py \
    --bundles /inbox/po-bundle-* \
    --by-basename --project vllm-mot \
    --out workspace/ara/discovered_logs.json
```

The **same repo usually lives at a different absolute path on each machine**, so
its transcripts carry different `cwd` labels. Reconcile them into one project:
- `--by-basename` unifies path-like labels by their last path component
  (`/data/a/vllm-mot` + `/home/b/vllm-mot` → `vllm-mot`), and
- `--alias "vllm-mot=vllm-mot,mot-router"` (repeatable) maps any label
  containing a substring to a canonical name — also collapses sub-directory
  sessions (`…/vllm-mot/router`) into the parent project.

Merge writes the standard manifest with two extra provenance keys per file
(`machine`, `project_original`) and a `by_machine` summary. **This replaces
Phase 1 + Phase 1.5** — skip straight to Phase 2 on the merged manifest.

> Single machine but you still want transcripts? Phase 0 works with **one**
> bundle too: `collect_machine.py` then `merge_bundles.py --bundles ./po-bundle`.

---

A pre-processing skill for PaperOrchestra (arXiv:2604.05018). Reads scattered
experimentation artifacts from AI coding-agent cache directories and synthesizes
them into the structured `(I, E)` input pair the PaperOrchestra pipeline expects.

```
[.claude/]  [.cursor/]  [.antigravity/]  [.openclaw/]
      │            │              │               │
      └────────────┴──────────────┴───────────────┘
                          │
                    Phase 1: Discovery
                  (discover_logs.py)
                          │
                    discovered_logs.json
                          │
                    Phase 2: Extraction
                  (LLM call per log batch)
                          │
                    raw_experiments.json
                          │
                    Phase 3: Synthesis
                  (LLM call — consolidate)
                          │
                    synthesis.json
                          │
                    Phase 4: Formatting
                  (format_po_inputs.py)
                          │
             ┌────────────┴────────────┐
      workspace/inputs/         workspace/ara/
        idea.md                   aggregation_report.md
        experimental_log.md       discovered_logs.json
                                  raw_experiments.json
                                  synthesis.json
```

The output drops directly into `workspace/inputs/` so the user can immediately
run `paper-orchestra` on the same workspace.

---

## Inputs

| Parameter | Required | Default | Description |
|---|---|---|---|
| `--search-roots` | no | cwd, `~` | Comma-separated directories to scan for agent caches |
| `--agents` | no | all | Comma-separated subset: `claude,cursor,antigravity,openclaw` |
| `--workspace` | no | `./workspace` | PaperOrchestra workspace root |
| `--depth` | no | 4 | Max directory scan depth (prevents runaway scans on large home dirs) |
| `--since` | no | none | Only include logs modified after this date (ISO 8601: `2025-01-01`) |

The user specifies these when invoking the skill, or you may ask them for
`--search-roots` if the current directory has no detectable agent caches.

---

## Phase 1 — Discovery (deterministic)

Run the discovery script to catalog every relevant log file:

```bash
python skills/agent-research-aggregator/scripts/discover_logs.py \
    --search-roots <roots> \
    --agents <agents> \
    --depth <depth> \
    --since <since> \
    --out workspace/ara/discovered_logs.json
```

The script exits with code **2** when no `--project` filter is set (this is
expected on the first run). It prints a **"Projects found"** list to stdout —
show it to the user immediately.

**If no logs are found at all:** stop and ask the user to specify
`--search-roots` or point you at a directory that contains agent cache folders.

---

## Phase 1.5 — Project Selection (mandatory)

**A paper can only be written from a single project. You must ask the user
which project to use before any LLM processing begins.**

1. Display the numbered project list from the discovery summary, e.g.:
   ```
   Projects found:
     [1] /home/alice/projects/my-rl-experiment  (42 files)
     [2] /home/alice/projects/llm-eval-suite    (17 files)
     [3] /home/alice/projects/old-demo          (3 files)
   ```
2. Ask: *"Which project should this paper be based on? Please choose a number
   or paste the project path."*
3. **Do not proceed to Phase 2 until the user has answered.**
4. Re-run discovery with the chosen project to filter the manifest:

```bash
python skills/agent-research-aggregator/scripts/discover_logs.py \
    --search-roots <roots> \
    --agents <agents> \
    --depth <depth> \
    --since <since> \
    --project "<chosen project path>" \
    --out workspace/ara/discovered_logs.json
```

This overwrites `discovered_logs.json` so only the selected project's files
remain. The script exits 0 on success.

**If the discovery finds only one project:** skip the question and inform the
user: *"Only one project found: `<path>`. Using it for the paper."* — then
re-run with `--project` automatically.

**If the discovery summary shows irrelevant files after filtering:** ask the
user whether to include or exclude them before continuing to Phase 2. Err on
the side of inclusion — the extraction prompt is conservative.

---

## Phase 2 — Extraction (LLM-assisted)

Process discovered logs in **batches** (group by agent type; keep batches under
~50 KB of raw text to stay within context limits):

For each batch:

1. **Read** the log files in the batch (the script's `--list` output tells you
   which file paths to read).
2. **Apply the extraction prompt** from `references/extraction-prompt.md` as
   your system message.
3. **Pass the raw log text** as the user message.
4. **Collect the structured JSON** the LLM returns (see schema in the prompt).
5. **Append** to `workspace/ara/raw_experiments.json`.

After all batches:

```bash
python skills/agent-research-aggregator/scripts/extract_experiments.py \
    --discovered workspace/ara/discovered_logs.json \
    --out workspace/ara/raw_experiments.json \
    --validate-only
```

Run this in `--validate-only` mode to check the combined JSON is well-formed
and meets the minimum schema (`experiments` array non-empty, each entry has
`hypothesis` or `method` or `results`). Fix any malformed entries before Phase 3.

---

## Phase 3 — Synthesis (LLM-assisted)

Consolidate possibly-redundant experiment records from multiple agent caches into
a single coherent research narrative. This is ONE LLM call.

**System message:** Use `references/synthesis-prompt.md` verbatim.

**User message:**
```
<raw_experiments>
{contents of workspace/ara/raw_experiments.json}
</raw_experiments>
```

The LLM must return a `synthesis.json` with keys:
- `research_question` — the overarching question being investigated
- `hypothesis` — the core proposed solution / claim
- `method_summary` — how the approach works (concise, no data leakage)
- `key_contributions` — 2–5 bullet strings
- `experimental_setup` — datasets, metrics, baselines, implementation notes
- `results_tables` — array of `{title, headers[], rows[]}` markdown-table objects
- `qualitative_observations` — free-form text blocks (what worked, what didn't,
  failure modes, ablation insights)
- `iteration_history` — ordered list of `{iteration_id, change_description,
  outcome}` entries if multiple iterations are detected
- `open_questions` — questions that remain unanswered in the logs

Save to `workspace/ara/synthesis.json`.

> **Note:** By this point, the user has already selected a single project in
> Phase 1.5. The synthesis should represent one coherent research thread. If
> the LLM still surfaces multiple disconnected research questions, flag this
> as a data quality warning in the audit report (Phase 5) but do not re-ask
> for project selection — that decision was made earlier.

---

## Phase 4 — Formatting (deterministic)

Convert `synthesis.json` into PaperOrchestra input files:

```bash
python skills/agent-research-aggregator/scripts/format_po_inputs.py \
    --synthesis workspace/ara/synthesis.json \
    --out workspace/inputs/
```

This generates two files:

### `workspace/inputs/idea.md` (Sparse variant)

Follows the PaperOrchestra Sparse Idea format (arXiv:2604.05018, §3.1):

```markdown
# [Synthesized Research Title]

## Problem
<2–4 sentence problem statement derived from research_question>

## Hypothesis
<hypothesis from synthesis>

## Method
<method_summary from synthesis>

## Key Contributions
<key_contributions as bullet list>

## Open Questions
<open_questions, if any>
```

### `workspace/inputs/experimental_log.md`

Follows the PaperOrchestra Experimental Log format (App. D.3):

```markdown
## 1. Experimental Setup
<experimental_setup from synthesis, formatted as prose + sub-bullets>

## 2. Raw Numeric Data
<results_tables converted to GitHub-Flavored Markdown tables>

## 3. Qualitative Observations
<qualitative_observations from synthesis>

### Iteration History
<iteration_history as an ordered narrative, if present>
```

After running the script, **review both files** with the user:

1. Read `workspace/inputs/idea.md` aloud and ask: "Does this accurately capture
   your research question and method?"
2. Read the table headers from `workspace/inputs/experimental_log.md` and ask:
   "Are these the correct metrics and baselines?"

Revise based on feedback before proceeding to PaperOrchestra.

---

## Phase 5 — Audit Report (deterministic)

```bash
python skills/agent-research-aggregator/scripts/format_po_inputs.py \
    --synthesis workspace/ara/synthesis.json \
    --out workspace/inputs/ \
    --report workspace/ara/aggregation_report.md
```

The `--report` flag makes the script also write `aggregation_report.md`, which
contains:

- Number of agent caches scanned, files read, batches processed
- Per-agent breakdown (files found per agent type)
- Experiment records extracted (count, date range)
- Iterations detected (count, convergence direction)
- Data quality warnings (gaps, low-confidence extractions, conflicting numbers)
- Files written and their sizes

Show the report to the user. If the data quality section lists warnings, discuss
them before running paper-orchestra — garbage in, garbage out.

---

## Handoff to PaperOrchestra

Once the user has confirmed `idea.md` and `experimental_log.md`, the workspace
is ready for the paper-orchestra pipeline. You still need:

| File | Status | Action |
|---|---|---|
| `workspace/inputs/idea.md` | ✓ generated | user review recommended |
| `workspace/inputs/experimental_log.md` | ✓ generated | user review recommended |
| `workspace/inputs/template.tex` | **MISSING** | ask user to provide their conference LaTeX template |
| `workspace/inputs/conference_guidelines.md` | **MISSING** | ask user to provide (page limit, deadline, formatting rules) |

Tell the user exactly which two files are still needed, then offer to run
`paper-orchestra` once they supply them.

---

## Error handling

| Situation | Action |
|---|---|
| Cache directory does not exist | Skip silently; note in report |
| File is binary or non-text | Skip; note in report |
| File > 200 KB | Truncate at 200 KB; note in report with path |
| LLM extraction returns malformed JSON | Re-prompt once with the parse error appended; if still malformed, log the batch as `status: failed` and continue |
| Synthesis returns > 1 `research_question` | Log as data quality warning in audit report; do not re-ask for project (was selected in Phase 1.5) |
| `results_tables` is empty after synthesis | Warn the user — PaperOrchestra's section-writing agent needs numeric data |

---

## Hard rules (never violate)

1. **Never write to agent cache directories.** This skill is read-only on `.claude/`, `.cursor/`, `.antigravity/`, `.openclaw/`.
2. **Never include personal information** (emails, names, credentials, API keys) in generated `idea.md` or `experimental_log.md`. The extraction prompt instructs the LLM to strip PII; double-check before handoff.
3. **Never fabricate results.** If a metric appears in only one log with low confidence, mark it `[UNVERIFIED]` in the table rather than silently including it.
4. **Never proceed past Phase 1 without user confirmation** of the discovered file list if the scan found > 50 files.

---

## Quick reference

```bash
# Phase 0 (optional: multi-machine and/or conversation transcripts)
#   on each machine:
python skills/agent-research-aggregator/scripts/collect_machine.py \
    --out ./po-bundle --search-roots ~/my-project --tar
#   ... scp the po-bundle-*.tar.gz files to one central machine ...
#   on the central machine (exits 2, then re-run with --project, exits 0):
python skills/agent-research-aggregator/scripts/merge_bundles.py \
    --bundles /inbox/po-bundle-* --by-basename \
    --out workspace/ara/discovered_logs.json
# (Phase 0 replaces Phase 1 + 1.5; jump to Phase 2 on the merged manifest.)

# Phase 1: discover all projects (exits with code 2 — project selection required)
python skills/agent-research-aggregator/scripts/discover_logs.py \
    --search-roots . ~ --out workspace/ara/discovered_logs.json

# Phase 1.5: re-run with chosen project (exits 0)
python skills/agent-research-aggregator/scripts/discover_logs.py \
    --search-roots . ~ \
    --project "/home/user/projects/my-chosen-project" \
    --out workspace/ara/discovered_logs.json

# ... (Phase 2: LLM extraction calls, see above) ...

python skills/agent-research-aggregator/scripts/extract_experiments.py \
    --discovered workspace/ara/discovered_logs.json \
    --out workspace/ara/raw_experiments.json --validate-only

# ... (Phase 3: LLM synthesis call, see above) ...

python skills/agent-research-aggregator/scripts/format_po_inputs.py \
    --synthesis workspace/ara/synthesis.json \
    --out workspace/inputs/ \
    --report workspace/ara/aggregation_report.md
```
