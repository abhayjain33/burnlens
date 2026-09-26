"""Run burnlens straight from a plugin checkout, without installing it."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from burnlens.cli import main  # noqa: E402

main()
