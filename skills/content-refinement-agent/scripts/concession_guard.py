#!/usr/bin/env python3
"""
concession_guard.py — Enforce the Devil's Advocate concession-threshold protocol
deterministically (references/da-reviewer.md).

The DA reviewer challenges the paper's core claims. A simulated reviewer left to
its own devices tends to *cave*: it concedes an attack as soon as the author
pushes back, even when the rebuttal is weak — sycophancy that defeats the point
of an adversarial reviewer. This script makes the protocol non-negotiable by
checking the DA's concession log against two hard rules, so the LLM cannot quietly
relax them:

  Rule 1 — Concession requires evidence.
      A finding may be conceded only if its rebuttal_score is >= 4 (the protocol's
      "rebuttal directly / strongly addresses the attack with evidence"). Conceding
      at rebuttal_score <= 3 is caving: the concession is REJECTED and the finding
      is restored to "standing".

  Rule 2 — No consecutive concessions (IRON RULE).
      The DA may make at most one valid concession per two review rounds.
      A concession in a round immediately following another conceding round is
      REJECTED and the finding is restored to "standing".

A CRITICAL finding that is still standing after these rules (not resolved by the
revision, and not validly conceded) BLOCKS the "refinement accepted" decision —
the host must treat the iteration as REVERT, regardless of rubric scores.

Concession log schema (--log):
    {
      "rounds": [
        {
          "round": 1,
          "findings": [
            {
              "id": "F1",
              "severity": "critical" | "major" | "minor",
              "attack": "Causal overclaiming: Sec 4 says X *causes* Y from corr only.",
              "rebuttal_score": 2,      # 1-5, DA's score of the author's rebuttal
              "conceded": false,        # did the DA drop the attack this round?
              "resolved": false         # was the underlying issue fixed in the revision?
            }
          ]
        }
      ]
    }

Usage:
    python concession_guard.py --log workspace/refinement/da_concessions.json
    python concession_guard.py --log da_concessions.json --out guard_report.json

Exit codes:
    0  CLEAR — no standing critical, no protocol violations → accept may proceed
    1  BLOCK — a critical finding is still standing → host must REVERT this iteration
    2  WARN  — protocol violation(s) found but no critical blocked → DA must restate;
              the rejected concession does not by itself force a revert
    3  input / schema error
"""
import argparse
import json
import sys

VALID_SEVERITY = {"critical", "major", "minor"}
CONCESSION_MIN_REBUTTAL = 4  # rebuttal_score >= this to allow a concession


def analyze(rounds: list) -> dict:
    violations = []
    valid_concessions = []
    standing_criticals = []
    last_conceded_round = None  # round index of the previous *valid* concession

    for r in rounds:
        rnum = r.get("round")
        conceded_this_round = False
        for fnd in r.get("findings", []):
            fid = fnd.get("id", "?")
            sev = fnd.get("severity", "minor")
            conceded = bool(fnd.get("conceded", False))
            resolved = bool(fnd.get("resolved", False))
            score = fnd.get("rebuttal_score")

            concession_valid = False
            if conceded:
                # Rule 1 — evidence threshold
                if score is None or score < CONCESSION_MIN_REBUTTAL:
                    violations.append({
                        "round": rnum, "id": fid, "type": "caving",
                        "detail": f"conceded at rebuttal_score={score} "
                                  f"(< {CONCESSION_MIN_REBUTTAL}); concession rejected",
                    })
                # Rule 2 — no consecutive concessions
                elif last_conceded_round is not None and rnum == last_conceded_round + 1:
                    violations.append({
                        "round": rnum, "id": fid, "type": "consecutive_concession",
                        "detail": f"concession in round {rnum} immediately follows a "
                                  f"concession in round {last_conceded_round}; "
                                  f"rejected (max one per two rounds)",
                    })
                else:
                    concession_valid = True
                    conceded_this_round = True
                    valid_concessions.append({"round": rnum, "id": fid, "severity": sev})

            # A critical is "standing" unless resolved OR validly conceded.
            if sev == "critical" and not resolved and not concession_valid:
                standing_criticals.append({
                    "round": rnum, "id": fid, "attack": fnd.get("attack", ""),
                })

        if conceded_this_round:
            last_conceded_round = rnum

    block = bool(standing_criticals)
    if block:
        action = "REVERT"
    elif violations:
        action = "DA_RESTATE"
    else:
        action = "PROCEED"

    return {
        "rounds_analyzed": len(rounds),
        "valid_concessions": valid_concessions,
        "violations": violations,
        "standing_criticals": standing_criticals,
        "block_accept": block,
        "recommended_action": action,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log", required=True, help="Concession log JSON path")
    p.add_argument("--out", default=None, help="Optional path to write the guard report JSON")
    args = p.parse_args()

    try:
        with open(args.log) as f:
            log = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: cannot read concession log: {e}", file=sys.stderr)
        return 3

    rounds = log.get("rounds")
    if not isinstance(rounds, list) or not rounds:
        print("ERROR: log['rounds'] must be a non-empty list", file=sys.stderr)
        return 3

    # Light schema validation — fail loudly rather than silently mis-classify.
    for r in rounds:
        for fnd in r.get("findings", []):
            sev = fnd.get("severity", "minor")
            if sev not in VALID_SEVERITY:
                print(f"ERROR: finding {fnd.get('id','?')} has invalid severity "
                      f"'{sev}' (expected one of {sorted(VALID_SEVERITY)})", file=sys.stderr)
                return 3
            score = fnd.get("rebuttal_score")
            if score is not None and not (1 <= score <= 5):
                print(f"ERROR: finding {fnd.get('id','?')} rebuttal_score={score} "
                      f"out of range 1-5", file=sys.stderr)
                return 3

    report = analyze(rounds)

    if args.out:
        with open(args.out, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

    # Human-readable summary.
    print(f"DA concession guard: {report['rounds_analyzed']} round(s)  "
          f"valid_concessions={len(report['valid_concessions'])}  "
          f"violations={len(report['violations'])}  "
          f"standing_criticals={len(report['standing_criticals'])}")
    for v in report["violations"]:
        print(f"  VIOLATION [{v['type']}] round {v['round']} {v['id']}: {v['detail']}")
    for c in report["standing_criticals"]:
        print(f"  STANDING CRITICAL round {c['round']} {c['id']}: {c['attack']}")
    print(f"  → recommended action: {report['recommended_action']}")

    if report["block_accept"]:
        return 1
    if report["violations"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
