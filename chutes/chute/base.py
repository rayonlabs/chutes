"""
Main application class, along with all of the inference decorators.
"""

import os
import asyncio
import uuid
from loguru import logger
from typing import Any, List
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict
from chutes.image import Image
from chutes.util.context import is_remote
from chutes.chute.node_selector import NodeSelector

if os.getenv("CHUTES_EXECUTION_CONTEXT") == "REMOTE":
    existing = os.getenv("NO_PROXY")
    os.environ["NO_PROXY"] = ",".join(
        [
            "localhost",
            "127.0.0.1",
            "api",
            "api.chutes.svc",
            "api.chutes.svc.cluster.local",
        ]
    )
    if existing:
        os.environ["NO_PROXY"] += f",{existing}"


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
        from chutes.chute.cord import Cord
        from chutes.chute.job import Job

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
        self._jobs: list[Job] = []
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
    def jobs(self):
        return self._jobs

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

        # Job methods.
        for job in self._jobs:
            logger.info(f"Found job definition: {job._func.__name__}")

    def cord(self, **kwargs):
        """
        Decorator to define a parachute cord (function).
        """
        from chutes.chute.cord import Cord

        cord = Cord(self, **kwargs)
        self._cords.append(cord)
        return cord

    def job(self, **kwargs):
        """
        Decorator to define a job.
        """
        from chutes.chute.job import Job

        job = Job(self, **kwargs)
        self._jobs.append(job)
        return job


# For returning things from the templates, aside from just a chute.
class ChutePack(BaseModel):
    chute: Chute
    model_config = ConfigDict(arbitrary_types_allowed=True)
