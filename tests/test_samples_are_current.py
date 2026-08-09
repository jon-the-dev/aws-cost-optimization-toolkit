"""Guard against committed sample output drifting away from the code that produces it."""

from __future__ import annotations

import generate_samples
import pytest


@pytest.mark.parametrize("filename", sorted(generate_samples.SAMPLES))
def test_committed_sample_matches_a_fresh_render(filename):
    expected = generate_samples.SAMPLES[filename]()
    actual = (generate_samples.OUTPUT_DIR / filename).read_text()

    assert actual == expected, f"{filename} is stale; run `make samples`"
