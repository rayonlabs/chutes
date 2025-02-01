import os
import aiohttp
import re
import backoff
import gzip
import time
import orjson as json
import fickling
import pickle
import pybase64 as base64
from pydantic import ValidationError
from typing import Optional, Dict, Any
from fastapi import Request, HTTPException, status
from loguru import logger
from contextlib import asynccontextmanager
from starlette.responses import StreamingResponse
from chutes.exception import InvalidPath, DuplicatePath, StillProvisioning
from chutes.util.context import is_local
from chutes.util.auth import sign_request
from chutes.util.schema import SchemaExtractor
from chutes.config import get_config
from chutes.constants import CHUTEID_HEADER, FUNCTION_HEADER
from chutes.chute.base import Chute
import chutes.metrics as metrics

# Simple regex to check for custom path overrides.
PATH_RE = re.compile(r"^(/[a-z0-9]+[a-z0-9-_]*)+$")


class Cord:
    def __init__(
        self,
        app: Chute,
        stream: bool = False,
        path: str = None,
        passthrough_path: str = None,
        passthrough: bool = False,
        passthrough_port: int = None,
        public_api_path: str = None,
        public_api_method: str = "POST",
        method: str = "POST",
        provision_timeout: int = 180,
        input_schema: Optional[Any] = None,
        minimal_input_schema: Optional[Any] = None,
        output_content_type: Optional[str] = None,
        output_schema: Optional[Dict] = None,
        **session_kwargs,
    ):
        """
        Constructor.
        """
        self._app = app
        self._path = None
        if path:
            self.path = path
        self._passthrough_path = None
        if passthrough_path:
            self.passthrough_path = passthrough_path
        self._public_api_path = None
        if public_api_path:
            self.public_api_path = public_api_path
        self._public_api_method = public_api_method
        self._passthrough_port = passthrough_port
        self._stream = stream
        self._passthrough = passthrough
        self._method = method
        self._session_kwargs = session_kwargs
        self._provision_timeout = provision_timeout
        self._config = None
        self.input_models = (
            [input_schema] if input_schema and hasattr(input_schema, "__fields__") else None
        )
        self.input_schema = (
            SchemaExtractor.get_minimal_schema(input_schema) if input_schema else None
        )
        self.minimal_input_schema = (
            SchemaExtractor.get_minimal_schema(minimal_input_schema)
            if minimal_input_schema
            else None
        )
        self.output_content_type = output_content_type
        self.output_schema = output_schema

    @property
    def path(self):
        """
        URL path getter.
        """
        return self._path

    @property
    def config(self):
        """
        Lazy config getter.
        """
        if self._config:
            return self._config
        self._config = get_config()
        return self._config

    @path.setter
    def path(self, path: str):
        """
        URL path setter with some basic validation.

        :param path: The path to use for the new endpoint.
        :type path: str

        """
        path = "/" + path.lstrip("/").rstrip("/")
        if "//" in path or not PATH_RE.match(path):
            raise InvalidPath(path)
        if any([cord.path == path for cord in self._app.cords]):
            raise DuplicatePath(path)
        self._path = path

    @property
    def passthrough_path(self):
        """
        Passthrough/upstream URL path getter.
        """
        return self._passthrough_path

    @passthrough_path.setter
    def passthrough_path(self, path: str):
        """
        Passthrough/usptream path setter with some basic validation.

        :param path: The path to use for the upstream endpoint.
        :type path: str

        """
        path = "/" + path.lstrip("/").rstrip("/")
        if "//" in path or not PATH_RE.match(path):
            raise InvalidPath(path)
        self._passthrough_path = path

    @property
    def public_api_path(self):
        """
        API path when using the hostname based invocation API calls.
        """
        return self._public_api_path

    @public_api_path.setter
    def public_api_path(self, path: str):
        """
        API path setter with basic validation.

        :param path: The path to use for the upstream endpoint.
        :type path: str

        """
        path = "/" + path.lstrip("/").rstrip("/")
        if "//" in path or not PATH_RE.match(path):
            raise InvalidPath(path)
        self._public_api_path = path

    @asynccontextmanager
    async def _local_call_base(self, *args, **kwargs):
        """
        Invoke the function from within the local/client side context, meaning
        we're actually just calling the chutes API.
        """
        logger.debug(f"Invoking remote function {self._func.__name__} via HTTP...")

        @backoff.on_exception(
            backoff.constant,
            (StillProvisioning,),
            jitter=None,
            interval=1,
            max_time=self._provision_timeout,
        )
        @asynccontextmanager
        async def _call():
            request_payload = {
                "args": base64.b64encode(gzip.compress(pickle.dumps(args))).decode(),
                "kwargs": base64.b64encode(gzip.compress(pickle.dumps(kwargs))).decode(),
            }
            dev_url = os.getenv("CHUTES_DEV_URL")
            headers, payload_string = {}, None
            if dev_url:
                payload_string = json.dumps(request_payload)
            else:
                headers, payload_string = sign_request(payload=request_payload)
            headers.update(
                {
                    CHUTEID_HEADER: self._app.uid,
                    FUNCTION_HEADER: self._func.__name__,
                }
            )
            base_url = dev_url or self.config.generic.api_base_url
            path = f"/chutes/{self._app.uid}{self.path}" if not dev_url else self.path
            async with aiohttp.ClientSession(base_url=base_url, **self._session_kwargs) as session:
                async with session.post(
                    path,
                    data=payload_string,
                    headers=headers,
                ) as response:
                    if response.status == 503:
                        logger.warning(f"Function {self._func.__name__} is still provisioning...")
                        raise StillProvisioning(await response.text())
                    elif response.status != 200:
                        logger.error(
                            f"Error invoking {self._func.__name__} [status={response.status}]: {await response.text()}"
                        )
                        raise Exception(await response.text())
                    yield response

        started_at = time.time()
        async with _call() as response:
            yield response
        logger.debug(
            f"Completed remote invocation [{self._func.__name__} passthrough={self._passthrough}] in {time.time() - started_at} seconds"
        )

    async def _local_call(self, *args, **kwargs):
        """
        Call the function from the local context, i.e. make an API request.
        """
        if os.getenv("CHUTES_DEV_URL"):
            async with self._local_call_base(*args, **kwargs) as response:
                return await response.read()
        result = None
        async for item in self._local_stream_call(*args, **kwargs):
            result = item
        return result

    async def _local_stream_call(self, *args, **kwargs):
        """
        Call the function from the local context, i.e. make an API request, but
        instead of just returning the response JSON, we're using a streaming
        response.
        """
        async with self._local_call_base(*args, **kwargs) as response:
            async for encoded_content in response.content:
                if (
                    not encoded_content
                    or not encoded_content.strip()
                    or not encoded_content.startswith(b"data: {")
                ):
                    continue
                content = encoded_content.decode()
                data = json.loads(content[6:])
                if data.get("trace"):
                    message = "".join(
                        [
                            data["trace"]["timestamp"],
                            " ["
                            + " ".join(
                                [
                                    f"{key}={value}"
                                    for key, value in data["trace"].items()
                                    if key not in ("timestamp", "message")
                                ]
                            ),
                            f"]: {data['trace']['message']}",
                        ]
                    )
                    logger.debug(message)
                elif data.get("error"):
                    logger.error(data["error"])
                    raise Exception(data["error"])
                elif data.get("result"):
                    if self._passthrough:
                        yield await self._func(data["result"])
                    else:
                        yield data["result"]

    @asynccontextmanager
    async def _passthrough_call(self, **kwargs):
        """
        Call a passthrough endpoint.
        """
        logger.debug(
            f"Received passthrough call, passing along to {self.passthrough_path} via {self._method}"
        )
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(connect=5.0, total=600.0),
            read_bufsize=8 * 1024 * 1024,
            base_url=f"http://127.0.0.1:{self._passthrough_port or 8000}",
        ) as session:
            async with getattr(session, self._method.lower())(
                self.passthrough_path, **kwargs
            ) as response:
                yield response

    async def _remote_call(self, request: Request, *args, **kwargs):
        """
        Function call from within the remote context, that is, the code that actually
        runs on the miner's deployment.
        """
        logger.info(
            f"Received invocation request [{self._func.__name__} passthrough={self._passthrough}]"
        )
        started_at = time.time()
        status = 200
        metrics.last_request_timestamp.labels(
            chute_id=self._app.uid,
            function=self._func.__name__,
        ).set_to_current_time()
        encrypt = getattr(request.state, "_encrypt", None)
        try:
            if self._passthrough:
                async with self._passthrough_call(**kwargs) as response:
                    logger.success(
                        f"Completed request [{self._func.__name__} passthrough={self._passthrough}] in {time.time() - started_at} seconds"
                    )
                    if encrypt:
                        return {"json": encrypt(await response.read())}
                    return await response.json()

            return_value = await self._func(self._app, *args, **kwargs)
            logger.success(
                f"Completed request [{self._func.__name__} passthrough={self._passthrough}] in {time.time() - started_at} seconds"
            )
            if hasattr(return_value, "body"):
                if encrypt:
                    return {
                        "type": response.__class__.__name__,
                        "status_code": response.status_code,
                        "headers": dict(response.headers),
                        "media_type": response.media_type,
                        "body": encrypt(response.body),
                    }
                else:
                    return return_value
            if encrypt:
                return {"json": encrypt(json.dumps(return_value))}
            return return_value
        except Exception as exc:
            logger.error(f"Error performing stream call: {exc}")
            status = 500
            raise
        finally:
            metrics.total_requests.labels(
                chute_id=self._app.uid,
                function=self._func.__name__,
                status=status,
            ).inc()
            metrics.request_duration.labels(
                chute_id=self._app.uid,
                function=self._func.__name__,
                status=status,
            ).observe(time.time() - started_at)

    async def _remote_stream_call(self, request: Request, *args, **kwargs):
        """
        Function call from within the remote context, that is, the code that actually
        runs on the miner's deployment.
        """
        logger.info(f"Received streaming invocation request [{self._func.__name__}]")
        status = 200
        started_at = time.time()
        metrics.last_request_timestamp.labels(
            chute_id=self._app.uid,
            function=self._func.__name__,
        ).set_to_current_time()
        encrypt = getattr(request.state, "_encrypt", None)
        try:
            if self._passthrough:
                async with self._passthrough_call(**kwargs) as response:
                    async for content in response.content:
                        if encrypt:
                            yield encrypt(content) + "\n"
                        else:
                            yield content
                logger.success(
                    f"Completed request [{self._func.__name__} (passthrough)] in {time.time() - started_at} seconds"
                )
                return

            async for data in self._func(self._app, *args, **kwargs):
                if encrypt:
                    yield encrypt(data) + "\n"
                else:
                    yield data
            logger.success(
                f"Completed request [{self._func.__name__}] in {time.time() - started_at} seconds"
            )
        except Exception as exc:
            logger.error(f"Error performing stream call: {exc}")
            status = 500
            raise
        finally:
            metrics.total_requests.labels(
                chute_id=self._app.uid,
                function=self._func.__name__,
                status=status,
            ).inc()
            metrics.request_duration.labels(
                chute_id=self._app.uid,
                function=self._func.__name__,
                status=status,
            ).observe(time.time() - started_at)

    async def _request_handler(self, request: Request):
        """
        Decode/deserialize incoming request and call the appropriate function.
        """
        if self._passthrough_port is None:
            self._passthrough_port = 8000
        args, kwargs = None, None
        if request.state.serialized:
            try:
                args = fickling.load(
                    gzip.decompress(base64.b64decode(request.state.decrypted["args"]))
                )
                kwargs = fickling.load(
                    gzip.decompress(base64.b64decode(request.state.decrypted["kwargs"]))
                )
            except fickling.exception.UnsafeFileError as exc:
                message = f"Detected potentially hazardous call arguments, blocking: {exc}"
                logger.error(message)
                raise HTTPException(
                    status_code=status.HTTP_401_FORBIDDEN,
                    detail=message,
                )
        else:
            # Dev mode hacks.
            if not self._passthrough:
                args = [request.state.decrypted]
                kwargs = {}
            else:
                args = []
                kwargs = {"json": request.state.decrypted}
        if not self._passthrough:
            if self.input_models and all([isinstance(args[idx], dict) for idx in range(len(args))]):
                try:
                    args = [
                        self.input_models[idx](**args[idx]) for idx in range(len(self.input_models))
                    ]
                except ValidationError:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid input parameters"
                    )

        if self._stream:
            return StreamingResponse(self._remote_stream_call(request, *args, **kwargs))
        return await self._remote_call(request, *args, **kwargs)

    def __call__(self, func):
        self._func = func
        if not self._path:
            self.path = func.__name__
        if not self._passthrough_path:
            self.passthrough_path = func.__name__
        if not self.input_models:
            self.input_models = SchemaExtractor.extract_models(func)
        in_schema, out_schema = SchemaExtractor.extract_schemas(func)
        if not self.input_schema:
            self.input_schema = in_schema
        if not self.output_schema:
            self.output_schema = out_schema
        if not self.output_content_type:
            if isinstance(out_schema, dict):
                if out_schema.get("type") == "object":
                    self.output_content_type = "application/json"
                else:
                    self.output_content_type = "text/plain"
        if is_local():
            return self._local_call if not self._stream else self._local_stream_call
        return self._remote_call if not self._stream else self._remote_stream_call
