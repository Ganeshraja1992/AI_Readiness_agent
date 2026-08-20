"""Command-line entry point: `ai-readiness run` (or `python -m ai_readiness_agent.cli run`)."""
from __future__ import annotations

import argparse
import json
import logging
import sys

from ai_readiness_agent.agent import AIReadinessAgent
from ai_readiness_agent.config import load_config


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-readiness", description="AI Readiness Agent")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run the full pipeline once and print the Assessment Result.")
    run_p.add_argument("--no-deliver", action="store_true", help="Skip sending to the Control Plane / outbox.")
    run_p.add_argument(
        "--use-case",
        type=str,
        default=None,
        help="AI use case to assess against (e.g. customer_support_agent). Normally supplied by the "
        "Control Plane's scan-trigger request; falls back to config for standalone runs.",
    )
    run_p.add_argument(
        "--environment-id",
        type=str,
        default=None,
        help="Customer environment ID this assessment belongs to (matches POST /assessment's environment_id).",
    )
    run_p.add_argument(
        "--assessment-id",
        type=str,
        default=None,
        help="Assessment ID assigned by the Control Plane (POST /assessment). If omitted, a local UUID is "
        "generated so the pipeline is runnable standalone.",
    )
    run_p.add_argument(
        "--out", type=str, default=None, help="Optional path to also write the full local result JSON to."
    )
    run_p.add_argument("-v", "--verbose", action="store_true", help="Verbose (DEBUG) logging.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    if args.command == "run":
        agent = AIReadinessAgent(load_config())
        result, receipt = agent.run(
            deliver=not args.no_deliver,
            use_case=args.use_case,
            environment_id=args.environment_id,
            assessment_id=args.assessment_id,
        )

        # Full local result (includes the Data Profile) — stays on this
        # machine; this is what gets printed/saved for the customer's own
        # visibility, NOT what crosses the Secure Result Channel.
        print(result.to_json())
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(result.to_json())

        # The minimal payload actually sent to the Control Plane, for demo
        # visibility of exactly what leaves the customer's environment.
        print("\n--- Control Plane payload (what actually crosses the Secure Result Channel) ---", file=sys.stderr)
        print(result.to_control_plane_payload().to_json(), file=sys.stderr)

        if receipt is not None:
            print(
                json.dumps(
                    {
                        "delivered": receipt.delivered,
                        "transport": receipt.transport,
                        "location": receipt.location,
                        "attempts": receipt.attempts,
                        "error": receipt.error,
                    },
                    indent=2,
                ),
                file=sys.stderr,
            )
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
