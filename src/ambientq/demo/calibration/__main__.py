"""Regenerate the shipped sample labeled set: ``python -m ambientq.demo.calibration [PATH]``."""

import sys

from ambientq.demo.calibration import SAMPLE_PATH, write_sample

print(write_sample(sys.argv[1] if len(sys.argv) > 1 else SAMPLE_PATH))
