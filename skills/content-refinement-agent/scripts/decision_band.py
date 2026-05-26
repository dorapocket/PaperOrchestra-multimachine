#!/usr/bin/env python3
"""
decision_band.py — Map a 0-100 overall score to a deterministic decision band.

The reviewer rubric (references/reviewer-rubric.md) produces a weighted 0-100
`overall_score` plus a free-form qualitative `decision`. That free-form label is
advisory; this script computes the *canonical* band the refinement loop reasons
about, so the band is reproducible and never drifts from the number it claims to
summarize.

Default bands (override with flags):
    >= 80          Accept
    65 .. 79       Minor Revision
    50 .. 64       Major Revision
    <  50          Reject

The bands give the loop an *absolute* quality target to complement the
*relative* accept/revert delta logic in score_delta.py: a paper can keep
improving relative to its previous self yet still sit in "Major Revision", and
conversely the loop can stop once it reaches "Accept" rather than burning
iterations chasing marginal gains.

Usage:
    python decision_band.py --score 74.6
    python decision_band.py --score-json workspace/refinement/iter2/score.json
    python decision_band.py --score 81 --accept-min 80 --minor-min 65 --major-min 50

Exit codes:
    0  band computed (always, on valid input)
    2  usage / input error
"""
import argparse
import json
import sys

DEFAULT_ACCEPT_MIN = 80
DEFAULT_MINOR_MIN = 65
DEFAULT_MAJOR_MIN = 50

ACCEPT = "Accept"
MINOR = "Minor Revision"
MAJOR = "Major Revision"
REJECT = "Reject"

# Ordered worst -> best, so callers can compare band strength numerically.
BAND_RANK = {REJECT: 0, MAJOR: 1, MINOR: 2, ACCEPT: 3}


def band_for(score: float,
             accept_min: float = DEFAULT_ACCEPT_MIN,
             minor_min: float = DEFAULT_MINOR_MIN,
             major_min: float = DEFAULT_MAJOR_MIN) -> str:
    """Return the decision band for an overall score. Importable by other scripts."""
    if score >= accept_min:
        return ACCEPT
    if score >= minor_min:
        return MINOR
    if score >= major_min:
        return MAJOR
    return REJECT


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--score", type=float, help="Overall score 0-100")
    src.add_argument("--score-json", help="Path to a score.json with an overall_score field")
    p.add_argument("--accept-min", type=float, default=DEFAULT_ACCEPT_MIN)
    p.add_argument("--minor-min", type=float, default=DEFAULT_MINOR_MIN)
    p.add_argument("--major-min", type=float, default=DEFAULT_MAJOR_MIN)
    args = p.parse_args()

    if not (args.accept_min > args.minor_min > args.major_min):
        print("ERROR: thresholds must satisfy accept-min > minor-min > major-min",
              file=sys.stderr)
        return 2

    if args.score is not None:
        score = args.score
    else:
        try:
            with open(args.score_json) as f:
                data = json.load(f)
            score = float(data["overall_score"])
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            print(f"ERROR: cannot read overall_score from {args.score_json}: {e}",
                  file=sys.stderr)
            return 2

    band = band_for(score, args.accept_min, args.minor_min, args.major_min)
    out = {
        "overall_score": score,
        "decision_band": band,
        "band_rank": BAND_RANK[band],
        "thresholds": {
            "accept_min": args.accept_min,
            "minor_min": args.minor_min,
            "major_min": args.major_min,
        },
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
