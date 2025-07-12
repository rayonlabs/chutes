import os
import re
import sys
import time
import hashlib
import aiohttp
import argparse
import mimetypes
import importlib
import importlib.util
from io import BytesIO
from functools import lru_cache
from loguru import logger
from typing import List, Dict, Any, Tuple
from chutes.config import get_config
from chutes.util.auth import sign_request
from fastapi import Request, status
from fastapi.responses import ORJSONResponse


CHUTE_REF_RE = re.compile(r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]+$", re.I)


@lru_cache(maxsize=1)
def miner():
    from graval import Miner

    return Miner()


class FakeStreamWriter:
    """
    Helper class for calculating sha256 for multi-part form posts (used in signature).
    """

    def __init__(self):
        self.output = BytesIO()

    async def write(self, chunk):
        self.output.write(chunk)

    async def drain(self):
        pass

    async def write_eof(self):
        pass


async def upload_logo(logo_path: str) -> str:
    """
    Upload a logo.
    """
    form_data = aiohttp.FormData()
    with open(logo_path, "rb") as infile:
        form_data.add_field(
            "logo",
            BytesIO(infile.read()),
            filename=os.path.basename(logo_path),
            content_type=mimetypes.guess_type(logo_path)[0] or "image/png",
        )
    payload = form_data()
    writer = FakeStreamWriter()
    await payload.write(writer)

    # Retrieve the raw bytes of the request body
    raw_data = writer.output.getvalue()
    async with aiohttp.ClientSession(
        base_url=get_config().generic.api_base_url, raise_for_status=True
    ) as session:
        headers, payload_string = sign_request(payload=raw_data)
        headers["Content-Type"] = payload.content_type
        headers["Content-Length"] = str(len(raw_data))
        async with session.post(
            "/logos/",
            data=raw_data,
            headers=headers,
        ) as response:
            return (await response.json())["logo_id"]


def parse_args(args: List[Any], args_config: Dict[str, Any]):
    """
    Parse the CLI args (or manual dict) to run the chute.
    """
    parser = argparse.ArgumentParser()
    for arg, kwargs in args_config.items():
        parser.add_argument(arg, **kwargs)
    return parser.parse_args(args)


def load_chute(
    chute_ref_str: str,
    config_path: str | None,
    debug: bool,
) -> Tuple[Any, Any]:
    """
    Load a chute from the chute ref string via dynamic imports and such.
    """

    if not CHUTE_REF_RE.match(chute_ref_str):
        logger.error(
            f"Invalid module name '{chute_ref_str}', usage: [module_name:chute_name] [args]"
        )
        sys.exit(1)

    # Config path updates.
    if config_path:
        os.environ["CHUTES_CONFIG_PATH"] = config_path

    # Debug logging?
    if not debug:
        logger.remove()
        logger.add(sys.stdout, level="INFO")

    from chutes.chute import Chute, ChutePack

    # Load the module.
    sys.path.append(os.getcwd())
    module_name, chute_name = chute_ref_str.split(":")
    try:
        spec = importlib.util.spec_from_file_location(
            module_name, os.getcwd() + f"/{module_name}.py"
        )
        if spec is None:
            raise ImportError(f"Cannot find module {module_name} in {os.getcwd()}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except ImportError as exc:
        logger.error(f"Unable to import module '{module_name}': {exc}")
        sys.exit(1)

    # Get the Chute reference (FastAPI server).
    try:
        chute = getattr(module, chute_name)
        if not isinstance(chute, (Chute, ChutePack)):
            logger.error(
                f"'{chute_name}' in module '{module_name}' is not of type Chute or ChutePack"
            )
            sys.exit(1)
    except AttributeError:
        logger.error(f"Unable to find chute '{chute_name}' in module '{module_name}'")
        sys.exit(1)

    return module, chute


async def authenticate_request(request: Request) -> tuple[bytes, ORJSONResponse]:
    """
    Request authentication via bittensor hotkey signatures.
    """
    miner_hotkey = request.headers.get("X-Chutes-Miner")
    validator_hotkey = request.headers.get("X-Chutes-Validator")
    nonce = request.headers.get("X-Chutes-Nonce")
    signature = request.headers.get("X-Chutes-Signature")
    if (
        any(not v for v in [miner_hotkey, validator_hotkey, nonce, signature])
        or validator_hotkey != miner()._validator_ss58
        or miner_hotkey != miner()._miner_ss58
        or int(time.time()) - int(nonce) >= 30
    ):
        logger.warning(f"Missing auth data: {request.headers}")
        return None, ORJSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "go away (missing)"},
        )
    body_bytes = await request.body() if request.method in ("POST", "PUT", "PATCH") else None
    payload_string = hashlib.sha256(body_bytes).hexdigest() if body_bytes else "chutes"
    signature_string = ":".join(
        [
            miner_hotkey,
            validator_hotkey,
            nonce,
            payload_string,
        ]
    )
    if not miner()._keypair.verify(signature_string, bytes.fromhex(signature)):
        return None, ORJSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "go away (sig)"},
        )
    return body_bytes, None
