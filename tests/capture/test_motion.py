import numpy as np

from ssc.capture.motion import frame_motion_reason


def star_field(*, elongated: bool = False) -> np.ndarray:
    rng = np.random.default_rng(42)
    yy, xx = np.indices((640, 640))
    image = rng.normal(100, 2, yy.shape)
    for y, x in rng.integers(30, 610, size=(55, 2)):
        amplitude = rng.uniform(500, 1500)
        sx = 6 if elongated else 1.8
        image += amplitude * np.exp(-0.5 * (((xx-x)/sx)**2 + ((yy-y)/1.8)**2))
    return image


def test_round_stars_and_inter_frame_dither_are_not_motion_damage() -> None:
    image = star_field()
    assert frame_motion_reason(image) is None
    assert frame_motion_reason(np.roll(image, (24, 100), (0, 1))) is None


def test_repeated_star_field_is_rejected() -> None:
    image = star_field()
    damaged = image + 0.45 * np.roll(image - 100, (4, 140), (0, 1))
    assert "repeated star field" in (frame_motion_reason(damaged) or "")


def test_coherent_smearing_is_rejected() -> None:
    assert "elongated" in (frame_motion_reason(star_field(elongated=True)) or "")


def test_starless_and_single_double_star_are_not_enough_evidence() -> None:
    image = np.zeros((128, 128))
    image[30, 30] = image[30, 80] = 1000
    assert frame_motion_reason(image) is None
    assert frame_motion_reason(np.zeros((128, 128))) is None
