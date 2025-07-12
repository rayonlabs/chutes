import os
import asyncio
import aiohttp
import sys
from loguru import logger
import typer
from typing import Any
from chutes.chute.base import Chute
from chutes.config import get_config
from chutes.entrypoint._shared import load_chute, upload_logo
from chutes.image import Image
from chutes.util.auth import sign_request
from chutes.chute import ChutePack


async def _deploy(
    ref_str: str, module: Any, chute: Chute, public: bool = False, logo_id: str = None
):
    """
    Perform the actual chute deployment.
    """
    confirm = input(
        f"\033[1m\033[4mYou are about to upload {module.__file__} and deploy {chute.name}, confirm? (y/n) \033[0m"
    )
    if confirm.lower().strip() != "y":
        logger.error("Aborting!")
        sys.exit(1)

    with open(module.__file__, "r") as infile:
        code = infile.read()
    config = get_config()
    request_body = {
        "name": chute.name,
        "tagline": chute.tagline,
        "readme": chute.readme,
        "logo_id": logo_id,
        "image": chute.image if isinstance(chute.image, str) else chute.image.uid,
        "public": public,
        "standard_template": chute.standard_template,
        "node_selector": chute.node_selector.dict(),
        "filename": os.path.basename(module.__file__),
        "ref_str": ref_str,
        "code": code,
        "concurrency": chute.concurrency,
        "cords": [
            {
                "method": cord._method,
                "path": cord.path,
                "public_api_path": cord.public_api_path,
                "public_api_method": cord._public_api_method,
                "stream": cord._stream,
                "function": cord._func.__name__,
                "input_schema": cord.input_schema,
                "output_schema": cord.output_schema,
                "output_content_type": cord.output_content_type,
                "minimal_input_schema": cord.minimal_input_schema,
                "passthrough": cord._passthrough,
            }
            for cord in chute._cords
        ],
        "jobs": [
            {
                "ports": [
                    {
                        "name": port.name,
                        "port": port.port,
                        "proto": port.proto,
                    }
                    for port in job.ports
                ],
                "timeout": job.timeout,
                "name": job._name,
                "upload": job.upload,
            }
            for job in chute._jobs
        ],
    }

    headers, request_string = sign_request(request_body)
    async with aiohttp.ClientSession(base_url=config.generic.api_base_url) as session:
        async with session.post(
            "/chutes/",
            data=request_string,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=None),
        ) as response:
            data = await response.json()
            if response.status in (409, 401):
                logger.error(f"{data['detail']}")
            elif response.status != 200:
                logger.error(f"Unexpected error deploying chute: {await response.text()}")
            else:
                logger.success(
                    f"Successfully deployed chute {chute.name} version={data['version']}, invocation will be available soon"
                )


async def _image_available(image: str | Image, public: bool) -> bool:
    """
    Check if an image exists and is built/published in the registry.
    """
    config = get_config()
    image_id = image if isinstance(image, str) else image.uid
    logger.debug(f"Checking if image_id={image_id} is available...")
    headers, _ = sign_request(purpose="images")
    async with aiohttp.ClientSession(base_url=config.generic.api_base_url) as session:
        async with session.get(
            f"/images/{image_id}",
            headers=headers,
        ) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("status") == "built and pushed":
                    if public and not data.get("public"):
                        logger.error("Unable to create public chutes from non-public images")
                        return False
                    return True
    return False


def deploy_chute(
    # TODO: needs to be a nicer way to do this
    chute_ref_str: str = typer.Argument(
        ...,
        help="The chute to deploy, either a path to a chute file or a reference to a chute on the platform",
    ),
    config_path: str = typer.Option(
        None, help="Custom path to the chutes config (credentials, API URL, etc.)"
    ),
    logo: str = typer.Option(
        None,
        help="Optional path to a logo to use for the chute",
    ),
    debug: bool = typer.Option(False, help="enable debug logging"),
    public: bool = typer.Option(False, help="mark an image as public/available to anyone"),
):
    """
    Deploy a chute to the platform.
    """

    async def _deploy_chute():
        nonlocal config_path, debug, public, chute_ref_str, logo
        module, chute = load_chute(chute_ref_str, config_path=config_path, debug=debug)

        # Get the image reference from the chute.
        chute = chute.chute if isinstance(chute, ChutePack) else chute

        # Ensure the image is ready to be used.
        if not await _image_available(chute.image, public):
            image_id = chute.image if isinstance(chute.image, str) else chute.image.uid
            logger.error(f"Image '{image_id}' is not available to be used (yet)!")
            sys.exit(1)

        # Upload logo, if any.
        logo_id = None
        if logo:
            logo_id = await upload_logo(logo)

        # Deploy!
        return await _deploy(chute_ref_str, module, chute, public, logo_id)

    return asyncio.run(_deploy_chute())
