"""Reference uploads are planning inputs, never device commands."""

import asyncio
from pathlib import Path

import pytest

from ssc.api import reference_image
from ssc.api.routes import skymap
from ssc.astro.frames import j2000_coord
from ssc.platesolve.astap import _read_date_obs
from ssc.platesolve.base import InvalidPlateSolveInputError, ReferencePlateSolveResult
from tests.api.conftest import FakeGateway, TestClient, services_of


def fits(width=2, height=2):
    cards = [
        "SIMPLE  =                    T",
        "BITPIX  =                    8",
        "NAXIS   =                    2",
        f"NAXIS1  = {width:20}",
        f"NAXIS2  = {height:20}",
        "END",
    ]
    return b"".join(c.ljust(80).encode() for c in cards).ljust(2880, b" ") + bytes(width * height)


@pytest.mark.parametrize("config", [True], indirect=True)
def test_unpaired_and_view_only_cannot_upload(client: TestClient):
    assert client.post("/api/skymap/solve-reference", content=fits()).status_code == 401
    token = client.get("/api/auth/pairing").json()["token"]
    assert client.post("/api/auth/pair", json={"token": token}).status_code == 200
    assert client.put("/api/auth/control-pin", json={"pin": "8421"}).status_code == 200
    assert client.post("/api/skymap/solve-reference", content=fits()).status_code == 403


@pytest.mark.parametrize(
    "payload",
    [
        b"not an image",
        fits()[:2881],
        fits(8000, 4001),
        b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + b"\0\0\0\2\0\0\0\2" + b"\0" * 9,
    ],
)
def test_invalid_images_rejected(paired: TestClient, payload):
    assert paired.post("/api/skymap/solve-reference", content=payload).status_code == 422


def test_byte_limit(paired: TestClient, monkeypatch):
    monkeypatch.setattr(skymap, "MAX_REFERENCE_BYTES", 10)
    assert paired.post("/api/skymap/solve-reference", content=fits()).status_code == 413


def test_success_exact_coordinates_idempotent_no_device_actions(
    paired: TestClient, gateway: FakeGateway, monkeypatch
):
    calls = []

    async def solver(path, **kwargs):
        calls.append(path)
        assert path.read_bytes() == fits()
        return ReferencePlateSolveResult(
            j2000_coord(23.123456 * 15, -45.654321), 2.3, 11.5, "astap", "test"
        )

    monkeypatch.setattr(reference_image, "astap_solve_reference", solver)
    response = paired.post(
        "/api/skymap/solve-reference", content=fits(), headers={"Idempotency-Key": "reference-test"}
    )
    assert response.status_code == 202
    operation = response.json()

    async def finished():
        task = services_of(paired).operations._running.get(operation["id"])
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)

    paired.portal.call(finished)
    operation = paired.get("/api/operations/" + operation["id"]).json()
    assert operation["status"] == "succeeded", operation
    assert operation["result"]["ra_j2000_h"] == 23.123456
    assert operation["result"]["dec_j2000_deg"] == -45.654321
    duplicate = paired.post(
        "/api/skymap/solve-reference",
        content=b"invalid",
        headers={"Idempotency-Key": "reference-test"},
    )
    assert duplicate.json()["id"] == operation["id"]
    assert len(calls) == 1 and not calls[0].parent.exists()
    assert gateway.mount_calls == [] and gateway.connect_calls == []
    assert not list(services_of(paired).config.data_dir.glob("reference-solve-*"))


@pytest.mark.asyncio
async def test_failure_cleans_private_image(paired: TestClient, monkeypatch):
    paths = []

    async def fail(path, **kwargs):
        paths.append(path)
        raise ValueError("No stars found")

    monkeypatch.setattr(reference_image, "astap_solve_reference", fail)
    services = services_of(paired)
    with pytest.raises(ValueError, match="No stars"):
        await reference_image.solve_reference_image(
            fits(), services.settings, services.config.data_dir
        )
    assert not paths[0].parent.exists()


def test_reference_fits_without_timestamp_does_not_relax_capture_guard(tmp_path: Path):
    assert reference_image.reference_image_suffix(fits()) == ".fits"
    path = tmp_path / "capture.fits"
    path.write_bytes(fits())
    with pytest.raises(InvalidPlateSolveInputError, match="DATE-OBS"):
        _read_date_obs(path)


@pytest.mark.parametrize("disconnect", [False, True])
def test_concurrent_upload_refused_before_stream_and_failed_upload_releases_slot(
    paired: TestClient, disconnect: bool
):
    from fastapi import HTTPException
    from starlette.requests import ClientDisconnect

    services = services_of(paired)

    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()
        second_consumed = False

        class SlowUpload:
            async def stream(self):
                entered.set()
                await release.wait()
                if disconnect:
                    raise ClientDisconnect()
                yield b"invalid"

        class SecondUpload:
            async def stream(self):
                nonlocal second_consumed
                second_consumed = True
                yield b"also invalid"

        first = asyncio.create_task(skymap.solve_reference(SlowUpload(), services, None, None))
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            assert services.reference_image_slot.locked()
            with pytest.raises(HTTPException) as blocked:
                await skymap.solve_reference(SecondUpload(), services, None, None)
            assert blocked.value.status_code == 409
            assert not second_consumed
        finally:
            release.set()
        if disconnect:
            with pytest.raises(ClientDisconnect):
                await asyncio.wait_for(first, timeout=2)
        else:
            with pytest.raises(HTTPException) as invalid:
                await asyncio.wait_for(first, timeout=2)
            assert invalid.value.status_code == 422
        assert not services.reference_image_slot.locked()
        with pytest.raises(HTTPException) as next_invalid:
            await skymap.solve_reference(SecondUpload(), services, None, None)
        assert next_invalid.value.status_code == 422
        assert second_consumed
        assert not services.reference_image_slot.locked()

    paired.portal.call(exercise)


@pytest.mark.asyncio
async def test_reference_deadline_is_15_seconds_and_timeout_is_user_error(paired, monkeypatch):
    from ssc.platesolve.base import SolveIssue, SolveTimedOutError
    paths = []
    async def timeout(path, **kwargs):
        assert kwargs["timeout_s"] == 15.0
        paths.append(path)
        raise SolveTimedOutError(SolveIssue.TIMED_OUT, "private solver diagnostic")
    monkeypatch.setattr(reference_image, "astap_solve_reference", timeout)
    services = services_of(paired)
    with pytest.raises(ValueError, match="^Reference image solving timed out[.]$"):
        await reference_image.solve_reference_image(fits(), services.settings, services.config.data_dir)
    assert not paths[0].parent.exists()
