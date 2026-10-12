# SPDX-License-Identifier: GPL-3.0-only
from dataclasses import replace

import numpy as np
import pytest

from ssc.capture.fits import write_fits_file
from ssc.skymap.fits import read_fits_image
from ssc.stacking.live import LiveStackEngine
from tests.capture.test_motion import star_field
from tests.stacking.test_live import _metadata


@pytest.mark.parametrize("explicit_path", [False, True])
def test_saved_fits_and_manifest_do_not_change_during_later_acquisition(tmp_path, explicit_path):
    engine = LiveStackEngine(tmp_path)
    engine.reset()
    pixels = star_field().astype(np.uint16)
    for index, group in enumerate(("a", "a", "b")):
        path = tmp_path / f"raw-{index}.fits"
        write_fits_file(path, pixels, replace(_metadata(), dither_group_id=group))
        engine.add_frame(path)
        if index == 0:
            # Include export to the initial working path, not just a new path.
            destination = engine.run.output_path if explicit_path else None
            saved = engine.save(output_path=destination)
            fits_before = saved.output_path.read_bytes()
            manifest_before = saved.parameters_path.read_bytes()
    assert len(engine.run.frame_paths) == 3
    assert read_fits_image(fits_before).header["NCOMBINE"] == 1
    assert saved.output_path.read_bytes() == fits_before
    assert saved.parameters_path.read_bytes() == manifest_before
    # A later explicit save updates both, and represents the new snapshot.
    updated = engine.save()
    assert read_fits_image(updated.output_path.read_bytes()).header["NCOMBINE"] == 3
