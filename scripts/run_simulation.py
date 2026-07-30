"""Compatibility wrapper for the repository's canonical simulation CLI."""
from __future__ import annotations

import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from main import _parser, main


if __name__ == "__main__":
    args = _parser().parse_args()
    main(
        config_path=args.config,
        steps=args.steps,
        port=args.port,
        start_server=not args.no_server,
        step_delay=args.step_delay,
        hold_server=args.hold_server,
    )
