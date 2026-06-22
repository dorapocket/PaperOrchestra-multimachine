#!/usr/bin/env python3
"""
merge_bundles.py — Phase 0b: merge per-machine bundles (run on the CENTRAL box).

Takes the bundles produced by collect_machine.py on each machine and merges them
into a single discovered_logs.json — the exact manifest schema that the rest of
the agent-research-aggregator pipeline (extract_experiments.py, Phase 2-4)
already consumes. After this step the multi-machine case is indistinguishable
from the single-machine case downstream.

It also:
  * copies every bundle's files into a stable, self-contained tree under
    <workspace>/ara/merged/<host>/ so the manifest's paths keep resolving even
    after you delete the original bundles,
  * tags every file with its source machine (provenance), and
  * reconciles the SAME project run on different machines (the repo usually
    lives at different absolute paths per box) into one project label, so
    Phase 1.5 sees one project instead of N.

Project reconciliation:
  default      group by exact project label
  --by-basename  unify path-like labels by their last path component
                 (e.g. /data/gl325/vllm-mot  +  /home/me/vllm-mot  ->  vllm-mot)
  --alias "canonical=sub1,sub2"  (repeatable) map any label containing one of
                 the substrings to <canonical>

Usage:
    # first pass — list merged projects (exits 2: choose one)
    python merge_bundles.py \\
        --bundles /inbox/po-bundle-gpu1.tar.gz /inbox/po-bundle-gpu2 \\
        --by-basename \\
        --out workspace/ara/discovered_logs.json

    # second pass — filter to the chosen project (exits 0)
    python merge_bundles.py \\
        --bundles /inbox/po-bundle-* \\
        --by-basename \\
        --project vllm-mot \\
        --out workspace/ara/discovered_logs.json
"""

import argparse
import json
import os
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _load_bundle(path: Path, stage: Path) -> Path | None:
    """Return a directory containing bundle_meta.json + manifest.json + files/.
    Extracts tarballs into `stage`."""
    if path.is_dir():
        if (path / "manifest.json").exists():
            return path
        # maybe a dir that holds an extracted bundle one level down
        subs = [d for d in path.iterdir() if (d / "manifest.json").exists()]
        return subs[0] if subs else None
    if path.is_file() and (path.name.endswith(".tar.gz") or path.name.endswith(".tgz")):
        dest = stage / path.name.replace(".tar.gz", "").replace(".tgz", "")
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(path, "r:gz") as tf:
            tf.extractall(dest)
        cands = [dest] + [d for d in dest.iterdir() if d.is_dir()]
        for c in cands:
            if (c / "manifest.json").exists():
                return c
    return None


def _canonicalizer(by_basename: bool, aliases: list[str]):
    parsed = []
    for a in aliases or []:
        if "=" in a:
            canon, subs = a.split("=", 1)
            parsed.append((canon.strip(), [s.strip() for s in subs.split(",") if s.strip()]))

    def canon(label: str) -> str:
        for canonical, subs in parsed:
            if any(s.lower() in label.lower() for s in subs):
                return canonical
        if by_basename and ("/" in label or "\\" in label):
            return os.path.basename(label.replace("\\", "/").rstrip("/")) or label
        return label

    return canon


def main():
    ap = argparse.ArgumentParser(description="Merge per-machine collection bundles")
    ap.add_argument("--bundles", nargs="+", required=True,
                    help="Bundle dirs and/or .tar.gz files (globs are fine)")
    ap.add_argument("--out", required=True, help="Output discovered_logs.json path")
    ap.add_argument("--merged-dir", default=None,
                    help="Where to assemble the unified file tree "
                         "(default: <out parent>/merged)")
    ap.add_argument("--by-basename", action="store_true",
                    help="Unify path-like project labels by their last component")
    ap.add_argument("--alias", action="append", default=[],
                    help="canonical=sub1,sub2 — map labels containing a substring")
    ap.add_argument("--project", default=None,
                    help="Filter to one (canonical) project label; omit to list & exit 2")
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    merged_dir = Path(args.merged_dir) if args.merged_dir else out_path.parent / "merged"
    merged_dir.mkdir(parents=True, exist_ok=True)
    canon = _canonicalizer(args.by_basename, args.alias)

    stage = Path(tempfile.mkdtemp(prefix="po_merge_"))
    all_entries: list[dict] = []
    hosts_seen: list[str] = []

    try:
        for raw in args.bundles:
            bpath = Path(raw).expanduser()
            broot = _load_bundle(bpath, stage)
            if not broot:
                print(f"[WARN] not a valid bundle, skipping: {bpath}", file=sys.stderr)
                continue
            meta = json.loads((broot / "bundle_meta.json").read_text(encoding="utf-8")) \
                if (broot / "bundle_meta.json").exists() else {}
            host = meta.get("node") or meta.get("host") or broot.name
            hosts_seen.append(host)
            manifest = json.loads((broot / "manifest.json").read_text(encoding="utf-8"))

            host_root = merged_dir / host
            for e in manifest.get("files", []):
                rel = e.get("path")  # bundle-relative
                src = broot / rel
                if not src.exists():
                    continue
                dest = host_root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    shutil.copy2(src, dest)
                orig_label = str(e.get("project", "unknown"))
                all_entries.append({
                    "path": str(dest.resolve()),
                    "agent": e.get("agent", "claude"),
                    "kind": e.get("kind", "file"),
                    "priority": e.get("priority", "MEDIUM"),
                    "size_bytes": e.get("size_bytes", dest.stat().st_size),
                    "truncated": e.get("truncated", False),
                    "machine": e.get("machine", host),
                    "project_original": orig_label,
                    "project": canon(orig_label),
                    "orig_path": e.get("orig_path"),
                })
    finally:
        pass  # keep stage until end; cleaned below

    # Sort: HIGH > MEDIUM > LOW
    prio = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    all_entries.sort(key=lambda e: prio.get(e["priority"], 9))

    # Indexes
    by_project: dict[str, list[dict]] = {}
    for e in all_entries:
        by_project.setdefault(e["project"], []).append(e)

    if args.project:
        kept = [e for e in all_entries if e["project"] == args.project]
        if not kept:
            print(f"[ERROR] no files for project '{args.project}'. Available:", file=sys.stderr)
            for p in sorted(by_project):
                print(f"  {p}", file=sys.stderr)
            shutil.rmtree(stage, ignore_errors=True)
            sys.exit(1)
        all_entries = kept

    by_agent: dict[str, int] = {}
    by_machine: dict[str, int] = {}
    for e in all_entries:
        by_agent[e["agent"]] = by_agent.get(e["agent"], 0) + 1
        by_machine[e["machine"]] = by_machine.get(e["machine"], 0) + 1

    manifest_out = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "source": "merge_bundles",
        "machines": sorted(set(hosts_seen)),
        "selected_project": args.project,
        "total_files": len(all_entries),
        "total_size_bytes": sum(e["size_bytes"] for e in all_entries),
        "by_agent": by_agent,
        "by_machine": by_machine,
        "by_project": {p: len(v) for p, v in by_project.items()},
        "files": all_entries,
    }
    out_path.write_text(json.dumps(manifest_out, indent=2), encoding="utf-8")
    shutil.rmtree(stage, ignore_errors=True)

    # --- summary (mirrors discover_logs.py UX, plus per-machine provenance) ---
    print("\n=== Merged Bundle Summary ===")
    print(f"Machines     : {', '.join(sorted(set(hosts_seen))) or 'none'}")
    print(f"Merged tree  : {merged_dir}")
    print()
    print("Projects found (across all machines):")
    for i, (proj, entries) in enumerate(sorted(by_project.items()), 1):
        machines = sorted({e["machine"] for e in entries})
        mark = " ◀ selected" if args.project and proj == args.project else ""
        print(f"  [{i}] {proj}  ({len(entries)} files; machines: {', '.join(machines)}){mark}")
    print()
    if args.project:
        print(f"Filtered to project: {args.project}")
        print(f"Total files  : {len(all_entries)}  ({manifest_out['total_size_bytes']/1024:.1f} KB)")
        print("By machine:")
        for m, n in sorted(by_machine.items()):
            print(f"  {m:24s} {n:4d} files")
        print(f"\nManifest written to: {out_path}")
        print("Next: proceed to Phase 2 (extraction) on this manifest.")
    else:
        print(f"\nManifest written to: {out_path}")
        print("[ACTION REQUIRED] Select a project, then re-run with "
              "--project <label> to filter.")
        sys.exit(2)


if __name__ == "__main__":
    main()
