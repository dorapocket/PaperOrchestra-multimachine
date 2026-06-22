#!/usr/bin/env python3
"""
collect_machine.py — Phase 0a: per-machine / per-folder collection.

Runs on ONE machine and packs that machine's Claude Code history for a project
into a small, portable *bundle* you copy to a central machine and merge
(merge_bundles.py).

You control grouping manually with two labels:
  --project-name <LABEL>   the project this bundle belongs to. ALL files in the
                           bundle get this label. Run collect once per folder and
                           give the same --project-name to folders that are the
                           same project (e.g. sibling git-worktree dirs, one per
                           branch). At merge time, same label = one project.
  --node <ID>              which machine this is (provenance only; does NOT affect
                           the paper). Defaults to the hostname.

Typical use — a project living in several worktree folders on one node, plus a
copy on another node, all merged into one project "myproj":

    # node gpu1 — collect each folder, same project name:
    python collect_machine.py --search-roots ~/proj-main --project-name myproj --node gpu1 --out b1 --tar
    python collect_machine.py --search-roots ~/proj-feat --project-name myproj --node gpu1 --out b2 --tar
    # node gpu2:
    python collect_machine.py --search-roots ~/proj      --project-name myproj --node gpu2 --out b3 --tar
    # central — merge whatever tarballs you have:
    python merge_bundles.py --bundles b1.tar.gz b2.tar.gz b3.tar.gz --project myproj --out workspace/ara/discovered_logs.json

With --project-name set, collection is scoped to --search-roots: only history
whose recorded working dir is under a search root is taken (so each per-folder
run grabs just that folder). Without it, the bundle keeps each project's own
path as its label and you select at merge time.

What goes in a bundle (curated high-signal artifacts, not a directory dump):
  * memory/*.md, CLAUDE.md                                           (copied)
  * task records ~/.claude/tasks/<uuid>/*.json                       (copied)
  * STRUCTURED result files (results*.json, metrics.json, eval.json,
    experiments*, ablation*, *.ipynb, run_*/train_* logs)           (copied)
  * conversation transcripts (~/.claude/projects/*/*.jsonl)          (DISTILLED)

NOT collected by default: raw bench logs (hundreds of generic *.log) — pass
--include-logs. Vendored/build dirs (.deps, third_party, cmake-build-*, …) are
pruned. Subagent sidechain transcripts are excluded (their final output is kept
as the Agent result in the main session); --include-subagents to add them.
"""

import argparse
import fnmatch
import json
import os
import re
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

# Set from --project-name: when present, every collected file gets this label.
_PROJECT_OVERRIDE: str | None = None

# Vendored / build dirs to prune on top of discover_logs.SKIP_DIRS so the
# recursive general-file scan doesn't drag in third-party notebooks/logs.
_EXTRA_SKIP = {".deps", "_deps", "third_party", "cmake-build-release",
               "cmake-build-debug", "vendor", "external", "submodules"}
# Directories whose name implies experiment output — generic logs/configs are
# only collected when they live under one of these (or at the search root).
_RESULTS_DIR = re.compile(
    r"(bench|result|eval|ablation|metric|prof|output|sweep|experiment|runs?|logs?)",
    re.IGNORECASE)


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "x"


def _label(raw) -> str:
    """The stored project label: the manual --project-name if given, else the
    file's own project path/label."""
    return _PROJECT_OVERRIDE or str(raw)


def _under_roots(path, roots) -> bool:
    """True if `path` is one of `roots` or lives under one of them."""
    if not path:
        return False
    try:
        rp = Path(path).resolve()
    except (OSError, ValueError):
        return False
    for r in roots:
        try:
            rp.relative_to(r)
            return True
        except ValueError:
            continue
    return False


def _project_for_transcript(meta: dict, jsonl: Path) -> str:
    """A transcript's own project = its recorded cwd if present, else the decoded
    Claude project dir name."""
    cwd = meta.get("cwd")
    if cwd:
        return cwd
    decoded = dl.decode_claude_project_path(jsonl.parent.name)
    return decoded or jsonl.parent.name


def collect_nontranscript(roots, agents, depth, since_dt, host):
    """Scan agent memory / CLAUDE.md files via discover_logs. (Transcripts,
    tasks, and general result files are handled by dedicated passes below.)"""
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
                    prio = "HIGH" if any(p in pattern for p in spec.get("priority_dirs", [])) else "MEDIUM"
                    for e in dl.scan_dir_glob(base, pattern, agent, prio, depth, since_dt):
                        e["project_source"] = dl.infer_project(Path(e["path"]), root, agent)
                        entries.append(e)
            for e in dl.scan_root_files(root, spec["root_files"], agent, since_dt):
                e["project_source"] = dl.infer_project(Path(e["path"]), root, agent)
                entries.append(e)
    # dedup by absolute path
    seen, out = set(), []
    for e in entries:
        if e["path"] in seen:
            continue
        seen.add(e["path"])
        e["machine"] = host
        e["kind"] = "file"
        e["project"] = _label(e["project_source"])
        out.append(e)
    # Transcripts (.jsonl) and tasks (~/.claude/tasks/) have dedicated passes.
    return [e for e in out
            if not e["path"].endswith(".jsonl") and "/tasks/" not in e["path"]]


def _session_project_map():
    """Map every session uuid -> its real working directory, read from the cwd
    recorded inside each transcript. Lets us label task files (which are keyed
    by session uuid) with the correct project even with --no-transcripts."""
    m = {}
    pd = Path.home() / ".claude" / "projects"
    if pd.exists():
        for jl in pd.rglob("*.jsonl"):
            cwd = dt.session_meta(jl).get("cwd")
            if cwd:
                m[jl.stem] = cwd
    return m


def collect_tasks(host, since_dt, uuid2project):
    """Collect ~/.claude/tasks/<session-uuid>/<n>.json — per-session task
    records (subject + description), which frequently hold validated
    quantitative findings. Returns (manifest_entries, {rel_path: text})."""
    base = Path.home() / ".claude" / "tasks"
    entries, payload = [], {}
    if not base.exists():
        return entries, payload
    for jf in sorted(base.glob("*/*.json")):
        try:
            st = jf.stat()
        except OSError:
            continue
        if since_dt and datetime.fromtimestamp(st.st_mtime, tz=timezone.utc) < since_dt:
            continue
        uuid = jf.parent.name
        source = uuid2project.get(uuid, "unknown")
        project = _label(source)
        try:
            text = jf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = f"files/{_safe(str(project))}/tasks/{_safe(uuid)}-{_safe(jf.stem)}.json"
        payload[rel] = text
        entries.append({
            "path": rel,
            "orig_path": str(jf),
            "agent": "claude",
            "kind": "task",
            "priority": "HIGH",
            "size_bytes": len(text.encode("utf-8")),
            "modified_iso": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            "truncated": False,
            "project": project,
            "project_source": source,
            "machine": host,
        })
    return entries, payload


def collect_general_recursive(roots, depth, since_dt, host, include_logs=False):
    """Find experiment result files under each search root. Conservative:

      * HIGH-value STRUCTURED artifacts taken recursively (vendored/build dirs
        pruned): results*.{json,csv,tsv}, metrics.json, eval.json,
        experiments*.{json,yaml}, ablation*, *.ipynb, run_*/train_* logs.
      * MEDIUM/LOW generic files (README, notes, config*, plain *.log) only at
        the search-root top level — as the original scan did.

    Raw bench logs under a results tree are collected only with include_logs."""
    skip = dl.SKIP_DIRS | _EXTRA_SKIP
    entries = []
    for root in roots:
        if not root.exists():
            continue
        base_parts = len(root.parts)
        for dp, dirs, files in os.walk(root):
            if len(Path(dp).parts) - base_parts >= depth:
                dirs[:] = []
            dirs[:] = [d for d in dirs if d not in skip]
            at_root = Path(dp) == root
            in_results = include_logs and bool(_RESULTS_DIR.search(dp))
            for fn in files:
                for pat, prio in dl.GENERAL_PATTERNS:
                    if not fnmatch.fnmatch(fn, pat):
                        continue
                    if prio == "HIGH" or at_root or in_results:
                        e = dl.file_entry(Path(dp) / fn, "general", prio, since_dt)
                        if e:
                            e["project_source"] = str(root)
                            e["project"] = _label(str(root))
                            e["machine"] = host
                            e["kind"] = "file"
                            entries.append(e)
                    break
    seen, out = set(), []
    for e in entries:
        if e["path"] in seen:
            continue
        seen.add(e["path"])
        out.append(e)
    return out


def _is_subagent(jl: Path) -> bool:
    """Subagent (sidechain) transcripts: filename agent-*.jsonl, or under a
    subagents/ dir. Usually redundant with the main session."""
    return jl.name.startswith("agent-") or "subagents" in jl.parts


def collect_transcripts(roots, since_dt, host, project_filter, scope_to_roots,
                        include_subagents, distill_kw, chunk_bytes):
    """Find ~/.claude/projects/*/*.jsonl (the real transcript store), distill
    each, and return (manifest_entries, {rel_path: distilled_markdown}).

    scope_to_roots: keep only sessions whose recorded cwd is under a search root
    (used for per-folder collection)."""
    proj_dirs = [Path.home() / ".claude" / "projects"]
    for root in roots:
        cand = root / ".claude" / "projects"
        if cand.exists() and cand not in proj_dirs:
            proj_dirs.append(cand)

    jsonls = []
    for pd in proj_dirs:
        if pd.exists():
            jsonls.extend(sorted(pd.rglob("*.jsonl")))
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
        source = _project_for_transcript(meta, jl)
        if scope_to_roots and not _under_roots(meta.get("cwd") or source, roots):
            continue
        if project_filter and project_filter.lower() not in str(source).lower():
            continue
        project = _label(source)
        branch = meta.get("git_branch")
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
                "project_source": source,
                "machine": host,
                "git_branch": branch,
                "part": f"{idx}/{len(parts)}",
                "session_meta": meta if idx == 1 else None,
            })
    return entries, payload


def main():
    ap = argparse.ArgumentParser(description="Per-machine/per-folder Claude Code history collector")
    ap.add_argument("--out", required=True, help="Bundle output directory")
    ap.add_argument("--search-roots", default=".",
                    help="Comma-separated folders to collect (default cwd)")
    ap.add_argument("--project-name", default=None,
                    help="Label ALL files in this bundle as this project. Use the "
                         "same name across folders/worktrees/nodes that are one "
                         "project. Implies scoping collection to --search-roots.")
    ap.add_argument("--node", "--host", dest="node", default=None,
                    help="Machine id (provenance only; default: hostname)")
    ap.add_argument("--match-roots", action="store_true",
                    help="Collect only history whose working dir is under a "
                         "--search-root (implied by --project-name)")
    ap.add_argument("--agents", default="claude,cursor,antigravity,openclaw")
    ap.add_argument("--depth", type=int, default=4)
    ap.add_argument("--since", default=None, help="ISO 8601; only files/sessions after this")
    ap.add_argument("--project", default=None,
                    help="(legacy) keep only source projects whose path contains "
                         "this substring; for explicit grouping prefer --project-name")
    ap.add_argument("--no-transcripts", action="store_true",
                    help="Skip conversation transcripts (collect memory/results only)")
    ap.add_argument("--no-tasks", action="store_true",
                    help="Skip ~/.claude/tasks/ task records")
    ap.add_argument("--include-logs", action="store_true",
                    help="Also collect generic logs/configs under a results-like "
                         "directory (can be hundreds of files; off by default)")
    ap.add_argument("--include-subagents", action="store_true",
                    help="Also distill subagent (sidechain) transcripts")
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
                         "many bytes per file (0 disables). Default 150000.")
    ap.add_argument("--tar", action="store_true", help="Also produce <out>.tar.gz")
    args = ap.parse_args()

    global _PROJECT_OVERRIDE
    _PROJECT_OVERRIDE = args.project_name
    scope = bool(args.project_name) or args.match_roots
    host = args.node or socket.gethostname()
    roots = [Path(r.strip()).expanduser().resolve() for r in args.search_roots.split(",")]
    agents = [a.strip() for a in args.agents.split(",")]
    since_dt = (datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
                if args.since else None)

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    (out / "files").mkdir(parents=True, exist_ok=True)

    # Filters applied to memory/result/task entries (transcripts filter inline).
    def _keep(e):
        if args.project:
            s = args.project.lower()
            if (s not in str(e.get("project_source", "")).lower()
                    and s not in str(e.get("orig_path") or e.get("path", "")).lower()):
                return False
        if scope and not _under_roots(e.get("project_source") or e.get("path"), roots):
            return False
        return True

    # --- memory / CLAUDE.md (agent caches) + general result files (repo) ---
    disk_entries = [e for e in collect_nontranscript(roots, agents, args.depth, since_dt, host) if _keep(e)]
    disk_entries += [e for e in collect_general_recursive(roots, args.depth, since_dt, host, args.include_logs) if _keep(e)]
    for e in disk_entries:
        src = Path(e["path"])
        rel = f"files/{_safe(str(e['project']))}/{e['agent']}/{_safe(src.name)}"
        dest = out / rel
        i = 1
        while dest.exists():               # avoid clobbering same-named files
            rel = f"files/{_safe(str(e['project']))}/{e['agent']}/{_safe(src.stem)}-{i}{src.suffix}"
            dest = out / rel
            i += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dest)
        except OSError:
            continue
        e["orig_path"] = e.pop("path")
        e["path"] = rel

    # --- tasks (~/.claude/tasks/<uuid>/*.json — per-session task records) ---
    task_entries, task_payload = ([], {})
    if not args.no_tasks:
        task_entries, task_payload = collect_tasks(host, since_dt, _session_project_map())
        task_entries = [e for e in task_entries if _keep(e)]
        for e in task_entries:
            (out / e["path"]).parent.mkdir(parents=True, exist_ok=True)
            (out / e["path"]).write_text(task_payload[e["path"]], encoding="utf-8")
    file_entries = disk_entries + task_entries

    # --- transcripts (distilled) ---
    tx_entries, tx_payload = ([], {})
    if not args.no_transcripts:
        distill_kw = dict(
            max_chars=args.max_chars, keep_thinking=not args.no_thinking,
            max_block_chars=args.max_block_chars, keep_results=args.keep_results,
            drop_tools=args.no_tools, keep_meta=not args.no_meta)
        tx_entries, tx_payload = collect_transcripts(
            roots, since_dt, host, args.project, scope, args.include_subagents,
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
        "node": host,
        "project_name": args.project_name,
        "search_roots": [str(r) for r in roots],
        "scoped_to_roots": scope,
        "agents": agents,
        "since": args.since,
        "include_transcripts": not args.no_transcripts,
        "include_logs": args.include_logs,
        "transcript_count": len(tx_entries),
        "task_count": len(task_entries),
        "file_count": len(file_entries),
        "max_chars": args.max_chars,
        "schema": "po-bundle/v1",
    }
    (out / "bundle_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # --- summary ---
    n_mem = sum(1 for e in disk_entries if e["agent"] == "claude")
    n_gen = sum(1 for e in disk_entries if e["agent"] == "general")
    print(f"=== Bundle: project '{args.project_name or '(per-folder labels)'}' on node '{host}' ===")
    print(f"Output dir   : {out}")
    print(f"Memory / CLAUDE.md  : {n_mem}")
    print(f"Result files        : {n_gen}"
          + ("" if args.include_logs else "   (structured only; --include-logs for raw logs)"))
    print(f"Task records        : {len(task_entries)}")
    n_sessions = sum(1 for e in tx_entries if e.get("orig_size_bytes", 0) > 0)
    print(f"Transcripts (distilled) : {n_sessions} session(s) → {len(tx_entries)} file(s)")
    raw = sum(e.get("orig_size_bytes", 0) for e in tx_entries)
    dist = sum(e["size_bytes"] for e in tx_entries)
    if raw:
        print(f"  transcripts: {raw/1048576:.1f} MB raw → {dist/1024:.1f} KB distilled "
              f"({dist/raw*100:.2f}%)")
    # source folders + branches that fed this bundle (provenance)
    sources = sorted({str(e.get("project_source")) for e in all_entries if e.get("project_source")})
    branches = sorted({e["git_branch"] for e in all_entries if e.get("git_branch")})
    print("Projects in this bundle:")
    for p, n in sorted(by_project.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4d}  {p}")
    if sources:
        print(f"Collected from folders: {', '.join(sources)}")
    if branches:
        print(f"Branches seen        : {', '.join(branches)}")

    if args.tar:
        tar_path = out.with_suffix(out.suffix + ".tar.gz") if out.suffix else Path(str(out) + ".tar.gz")
        with tarfile.open(tar_path, "w:gz") as tf:
            tf.add(out, arcname=out.name)
        print(f"\nTarball      : {tar_path}  ({tar_path.stat().st_size/1048576:.1f} MB)")
        print(f"Copy it to the central machine, e.g.:\n  scp {tar_path} central:/inbox/")


if __name__ == "__main__":
    main()
