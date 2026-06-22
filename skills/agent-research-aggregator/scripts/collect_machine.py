#!/usr/bin/env python3
"""
collect_machine.py — Phase 0a: per-machine collection (run on EACH machine).

When experiments are run through Claude Code on several machines, the history is
scattered across each box's ~/.claude. This script runs ON one machine and packs
that machine's relevant artifacts into a small, portable, self-describing
*bundle* that you copy to a central machine for merging (Phase 0b,
merge_bundles.py).

What goes in a bundle:
  * memory/*.md, CLAUDE.md, todos, task-outputs        (copied verbatim)
  * general result files (results*.json, metrics.json, *.ipynb, …)  (copied)
  * conversation transcripts (~/.claude/projects/*/*.jsonl)          (DISTILLED)

Transcripts are distilled (see distill_transcript.py) before they enter the
bundle: raw transcripts on this machine total tens to hundreds of MB; distilled
they are ~2% of that, so a bundle stays small enough to scp and small enough to
feed to the extractor without blowing context. Pass --no-transcripts to skip
them and collect only memory/result files (the original behaviour).

Every file in the bundle is tagged with this machine's host id so provenance
survives the merge.

Transport is up to you — the bundle is just a directory (or a .tar.gz). Examples:
    scp -r po-bundle-<host>-<date>.tar.gz central:/inbox/
    rsync -a po-bundle-<host>-<date>/ central:/inbox/po-bundle-<host>/

Usage (on each machine):
    python collect_machine.py --out ./po-bundle --tar
    python collect_machine.py --out ./po-bundle --project vllm-mot --since 2026-01-01
    python collect_machine.py --out ./po-bundle --no-transcripts
"""

import argparse
import json
import os
import shutil
import socket
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

# Reuse the tested scan logic + distiller from sibling scripts.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discover_logs as dl          # noqa: E402
import distill_transcript as dt     # noqa: E402


def _safe(s: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "x"


def _project_for_transcript(meta: dict, jsonl: Path) -> str:
    """Canonical project label for a transcript = its recorded cwd if present,
    else the decoded Claude project dir name."""
    cwd = meta.get("cwd")
    if cwd:
        return cwd
    decoded = dl.decode_claude_project_path(jsonl.parent.name)
    return decoded or jsonl.parent.name


def collect_nontranscript(roots, agents, depth, since_dt, host):
    """Scan memory / CLAUDE.md / todos / general files via discover_logs."""
    entries = []
    for root in roots:
        if not root.exists():
            continue
        for agent, spec in dl.AGENT_SPECS.items():
            if agent not in agents:
                continue
            dirs = [root / c for c in spec["cache_dirs"] if (root / c).exists()]
            dirs += [Path(g) for g in spec["global_dirs"] if Path(g).exists()]
            for base in dirs:
                for pattern in spec["patterns"]:
                    # transcripts are handled separately; skip jsonl-ish here
                    prio = "HIGH" if any(p in pattern for p in spec.get("priority_dirs", [])) else "MEDIUM"
                    for e in dl.scan_dir_glob(base, pattern, agent, prio, depth, since_dt):
                        e["project"] = dl.infer_project(Path(e["path"]), root, agent)
                        entries.append(e)
            for e in dl.scan_root_files(root, spec["root_files"], agent, since_dt):
                e["project"] = dl.infer_project(Path(e["path"]), root, agent)
                entries.append(e)
        for e in dl.scan_general(root, depth, since_dt):
            e["project"] = dl.infer_project(Path(e["path"]), root, "general")
            entries.append(e)
    # dedup by absolute path
    seen, out = set(), []
    for e in entries:
        if e["path"] in seen:
            continue
        seen.add(e["path"])
        e["machine"] = host
        e["kind"] = "transcript" if e["path"].endswith(".jsonl") else "file"
        out.append(e)
    # Drop any stray .jsonl that slipped through a glob — transcripts are
    # collected (and distilled) in the dedicated pass below.
    return [e for e in out if not e["path"].endswith(".jsonl")]


def _is_subagent(jl: Path) -> bool:
    """Subagent (sidechain) transcripts: filename agent-*.jsonl, or under a
    subagents/ dir. These are usually redundant with the main session."""
    return jl.name.startswith("agent-") or "subagents" in jl.parts


def collect_transcripts(roots, since_dt, host, project_filter,
                        include_subagents, distill_kw, chunk_bytes):
    """Find ~/.claude/projects/*/*.jsonl (the real transcript store), distill
    each, and return (manifest_entries, {rel_path: distilled_markdown})."""
    # Claude Code stores transcripts in the GLOBAL projects dir regardless of
    # which project root you ran in. Also honour any per-root .claude/projects.
    proj_dirs = [Path.home() / ".claude" / "projects"]
    for root in roots:
        cand = root / ".claude" / "projects"
        if cand.exists() and cand not in proj_dirs:
            proj_dirs.append(cand)

    jsonls = []
    for pd in proj_dirs:
        if pd.exists():
            jsonls.extend(sorted(pd.rglob("*.jsonl")))
    # unique
    jsonls = list(dict.fromkeys(jsonls))
    if not include_subagents:
        jsonls = [j for j in jsonls if not _is_subagent(j)]

    entries, payload = [], {}
    for jl in jsonls:
        try:
            st = jl.stat()
        except OSError:
            continue
        if since_dt and datetime.fromtimestamp(st.st_mtime, tz=timezone.utc) < since_dt:
            continue
        meta = dt.session_meta(jl)
        project = _project_for_transcript(meta, jl)
        if project_filter and project_filter.lower() not in project.lower():
            continue
        md = dt.distill_session(jl, **distill_kw)
        # Keep each emitted file within the pipeline's per-file size budget by
        # splitting a long session into parts — no content is dropped.
        parts = dt.split_markdown(md, chunk_bytes)
        for idx, part in enumerate(parts, 1):
            suffix = "" if len(parts) == 1 else f".part{idx:02d}"
            rel = f"files/{_safe(project)}/transcripts/{_safe(jl.stem)}{suffix}.md"
            payload[rel] = part
            entries.append({
                "path": rel,                  # bundle-relative; merge resolves it
                "orig_path": str(jl),
                "agent": "claude",
                "kind": "transcript",
                "priority": "HIGH",
                "size_bytes": len(part.encode("utf-8")),
                "orig_size_bytes": st.st_size if idx == 1 else 0,
                "modified_iso": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                "truncated": False,
                "project": project,
                "machine": host,
                "part": f"{idx}/{len(parts)}",
                "session_meta": meta if idx == 1 else None,
            })
    return entries, payload


def main():
    ap = argparse.ArgumentParser(description="Per-machine Claude Code history collector")
    ap.add_argument("--out", required=True, help="Bundle output directory")
    ap.add_argument("--search-roots", default=".",
                    help="Comma-separated roots for memory/result files (default cwd)")
    ap.add_argument("--agents", default="claude,cursor,antigravity,openclaw")
    ap.add_argument("--depth", type=int, default=4)
    ap.add_argument("--since", default=None, help="ISO 8601; only files/sessions after this")
    ap.add_argument("--project", default=None,
                    help="Only keep projects whose label contains this substring")
    ap.add_argument("--host", default=None, help="Machine id (default: hostname)")
    ap.add_argument("--no-transcripts", action="store_true",
                    help="Skip conversation transcripts (collect memory/results only)")
    ap.add_argument("--include-subagents", action="store_true",
                    help="Also distill subagent (sidechain) transcripts "
                         "(agent-*.jsonl); default keeps only main sessions")
    ap.add_argument("--no-thinking", action="store_true",
                    help="Drop assistant reasoning from distilled transcripts")
    ap.add_argument("--no-tools", action="store_true",
                    help="Drop the compact tool trace (keep prose only)")
    ap.add_argument("--no-meta", action="store_true",
                    help="Drop system recap summaries (away_summary etc.)")
    ap.add_argument("--keep-results", type=int, default=dt.DEFAULT_KEEP_RESULTS,
                    help="Keep first N chars of each tool result (0 = drop, default)")
    ap.add_argument("--max-chars", type=int, default=dt.DEFAULT_MAX_CHARS,
                    help="Per-session distilled cap (0 = unlimited, default)")
    ap.add_argument("--max-block-chars", type=int, default=dt.DEFAULT_MAX_BLOCK,
                    help="Cap a single content block (0 = full, default)")
    ap.add_argument("--chunk-bytes", type=int, default=150_000,
                    help="Split a long session's distilled output into <= this "
                         "many bytes per file (keeps each within the extraction "
                         "budget; 0 disables). Default 150000.")
    ap.add_argument("--tar", action="store_true", help="Also produce <out>.tar.gz")
    args = ap.parse_args()

    host = args.host or socket.gethostname()
    roots = [Path(r.strip()).expanduser().resolve() for r in args.search_roots.split(",")]
    agents = [a.strip() for a in args.agents.split(",")]
    since_dt = (datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
                if args.since else None)

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    (out / "files").mkdir(parents=True, exist_ok=True)

    # --- non-transcript files (copied verbatim) ---
    file_entries = collect_nontranscript(roots, agents, args.depth, since_dt, host)
    if args.project:
        file_entries = [e for e in file_entries
                        if args.project.lower() in str(e.get("project", "")).lower()]
    for e in file_entries:
        src = Path(e["path"])
        rel = f"files/{_safe(str(e['project']))}/{e['agent']}/{_safe(src.name)}"
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dest)
        except OSError:
            continue
        e["orig_path"] = e.pop("path")
        e["path"] = rel

    # --- transcripts (distilled) ---
    tx_entries, tx_payload = ([], {})
    if not args.no_transcripts:
        distill_kw = dict(
            max_chars=args.max_chars, keep_thinking=not args.no_thinking,
            max_block_chars=args.max_block_chars, keep_results=args.keep_results,
            drop_tools=args.no_tools, keep_meta=not args.no_meta)
        tx_entries, tx_payload = collect_transcripts(
            roots, since_dt, host, args.project, args.include_subagents,
            distill_kw, args.chunk_bytes)
        for rel, md in tx_payload.items():
            dest = out / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(md, encoding="utf-8")

    all_entries = file_entries + tx_entries

    by_project = {}
    for e in all_entries:
        by_project.setdefault(str(e["project"]), 0)
        by_project[str(e["project"])] += 1

    manifest = {
        "files": all_entries,
        "total_files": len(all_entries),
        "total_size_bytes": sum(e["size_bytes"] for e in all_entries),
        "by_project": by_project,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    meta = {
        "host": host,
        "search_roots": [str(r) for r in roots],
        "agents": agents,
        "since": args.since,
        "project_filter": args.project,
        "include_transcripts": not args.no_transcripts,
        "transcript_count": len(tx_entries),
        "file_count": len(file_entries),
        "max_chars": args.max_chars,
        "schema": "po-bundle/v1",
    }
    (out / "bundle_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # --- summary ---
    print(f"=== Bundle for host '{host}' ===")
    print(f"Output dir   : {out}")
    print(f"Memory/result files : {len(file_entries)}")
    n_sessions = sum(1 for e in tx_entries if e.get("orig_size_bytes", 0) > 0)
    print(f"Transcripts (distilled) : {n_sessions} session(s) "
          f"→ {len(tx_entries)} file(s)")
    raw = sum(e.get("orig_size_bytes", 0) for e in tx_entries)
    dist = sum(e["size_bytes"] for e in tx_entries)
    if raw:
        print(f"  transcripts: {raw/1048576:.1f} MB raw → {dist/1024:.1f} KB distilled "
              f"({dist/raw*100:.2f}%)")
    print("Projects in this bundle:")
    for p, n in sorted(by_project.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4d}  {p}")

    if args.tar:
        tar_path = out.with_suffix(out.suffix + ".tar.gz") if out.suffix else Path(str(out) + ".tar.gz")
        with tarfile.open(tar_path, "w:gz") as tf:
            tf.add(out, arcname=out.name)
        print(f"\nTarball      : {tar_path}  ({tar_path.stat().st_size/1048576:.1f} MB)")
        print(f"Copy it to the central machine, e.g.:\n  scp {tar_path} central:/inbox/")


if __name__ == "__main__":
    main()
