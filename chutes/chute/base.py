"""
Main application class, along with all of the inference decorators.
"""

import asyncio
import aiohttp
import uuid
import orjson as json
from loguru import logger
from typing import Any, List, Dict
from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict
from chutes.image import Image
from chutes.util.context import is_remote
from chutes.chute.node_selector import NodeSelector

# NOTE: Alternative is to combine the modules
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chutes.chute.cord import Cord


async def _pong(request: Request) -> Dict[str, Any]:
    """
    Echo incoming request as a liveness check.
    """
    if hasattr(request.state, "_encrypt"):
        return {"json": request.state._encrypt(json.dumps(request.state.decrypted))}
    return request.state.decrypted


async def _get_token(request: Request) -> Dict[str, Any]:
    """
    Fetch a token, useful in detecting proxies between the real deployment and API.
    """
    endpoint = request.state.decrypted.get(
        "endpoint", "https://api.chutes.ai/instances/token_check"
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(endpoint) as resp:
            if hasattr(request.state, "_encrypt"):
                return {"json": await resp.json()}
            return await resp.json()


class Chute(FastAPI):
    def __init__(
        self,
        username: str,
        name: str,
        image: str | Image,
        tagline: str = "",
        readme: str = "",
        standard_template: str = None,
        node_selector: NodeSelector = None,
        concurrency: int = 1,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._username = username
        self._name = name
        self._readme = readme
        self._tagline = tagline
        self._uid = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{username}::chute::{name}"))
        self._image = image
        self._standard_template = standard_template
        self._node_selector = node_selector
        self._startup_hooks = []
        self._shutdown_hooks = []
        self._cords: list[Cord] = []
        self.concurrency = concurrency
        self.docs_url = None
        self.redoc_url = None

    @property
    def name(self):
        return self._name

    @property
    def readme(self):
        return self._readme

    @property
    def tagline(self):
        return self._tagline

    @property
    def uid(self):
        return self._uid

    @property
    def image(self):
        return self._image

    @property
    def cords(self):
        return self._cords

    @property
    def node_selector(self):
        return self._node_selector

    @property
    def standard_template(self):
        return self._standard_template

    def _on_event(self, hooks: List[Any]):
        """
        Decorator to register a function for an event type, e.g. startup/shutdown.
        """

        def decorator(func):
            if asyncio.iscoroutinefunction(func):

                async def async_wrapper(*args, **kwargs):
                    return await func(self, *args, **kwargs)

                hooks.append(async_wrapper)
                return async_wrapper
            else:

                def sync_wrapper(*args, **kwargs):
                    func(self, *args, **kwargs)

                hooks.append(sync_wrapper)
                return sync_wrapper

        return decorator

    def on_startup(self):
        """
        Wrapper around _on_event for startup events.
        """
        return self._on_event(self._startup_hooks)

    def on_shutdown(self):
        """
        Wrapper around _on_event for shutdown events.
        """
        return self._on_event(self._shutdown_hooks)

    async def initialize(self):
        """
        Initialize the application based on the specified hooks.
        """
        if not is_remote():
            return
        for hook in self._startup_hooks:
            if asyncio.iscoroutinefunction(hook):
                await hook()
            else:
                hook()

        # Add all of the API endpoints.
        for cord in self._cords:
            self.add_api_route(cord.path, cord._request_handler, methods=["POST"])
            logger.info(f"Added new API route: {cord.path} calling {cord._func.__name__}")
            logger.debug(f"  {cord.input_schema=}")
            logger.debug(f"  {cord.minimal_input_schema=}")
            logger.debug(f"  {cord.output_content_type=}")
            logger.debug(f"  {cord.output_schema=}")

        # Add a ping endpoint for validators to use.
        self.add_api_route("/_ping", _pong, methods=["POST"])
        logger.info("Added ping endpoint: /_ping")

        # Token fetch endpoint.
        self.add_api_route("/_token", _get_token, methods=["GET"])
        logger.info("Added token endpoint: /_token")

        # Add a k8s liveness check endpoint.
        self.add_api_route("/_alive", lambda: {"alive": True}, methods=["GET"])
        logger.info("Added liveness endpoint: /_alive")

    def cord(self, **kwargs):
        """
        Decorator to define a parachute cord (function).
        """
        from chutes.chute.cord import Cord

        cord = Cord(self, **kwargs)
        self._cords.append(cord)
        return cord


# For returning things from the templates, aside from just a chute.
class ChutePack(BaseModel):
    chute: Chute
    model_config = ConfigDict(arbitrary_types_allowed=True)
