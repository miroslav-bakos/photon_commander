"""The current-stack image route must not pin a target across handoff."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from ssc.api.routes.capture import live_stack_image


async def test_current_stack_image_is_not_immutable_cached():
    capture = SimpleNamespace(render_live_stack_preview=AsyncMock(return_value=b"png"))
    response = await live_stack_image(SimpleNamespace(capture=capture), auto_crop=True)
    assert response.body == b"png"
    assert response.headers["cache-control"] == "no-store"
