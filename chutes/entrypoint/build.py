import asyncio
import aiohttp
import os
import sys
import shutil
import importlib
import tempfile
import base64
import pickle
import orjson as json
from io import BytesIO
from contextlib import contextmanager
from loguru import logger
import typer

from chutes.config import get_config
from chutes.image import Image
from chutes.image.directive.add import ADD
from chutes.image.directive.generic_run import RUN
from chutes.entrypoint._shared import load_chute, FakeStreamWriter, upload_logo
from chutes.util.auth import sign_request


def expand_context_files(paths, cwd):
    files = []
    for path in paths:
        abs_path = os.path.abspath(path)
        if os.path.isdir(abs_path):
            for root, _, fs in os.walk(abs_path):
                for f in fs:
                    files.append(os.path.join(root, f))
        elif os.path.isfile(abs_path):
            files.append(abs_path)
    files = [f for f in files if os.path.commonpath([cwd, f]) == cwd]
    return files


@contextmanager
def temporary_build_directory(image):
    """
    Helper to copy the build context files to a build directory.
    """
    all_input_files = []
    for directive in image._directives:
        all_input_files += directive._build_context

    cwd = os.getcwd()
    all_real_files = [
        path
        for path in expand_context_files(all_input_files, cwd)
        if os.path.basename(path) != "Dockerfile"
    ]
    samples = all_real_files[:10]
    logger.info(
        f"Found {len(all_real_files)} files to include in build context -- \033[1m\033[4mthese will be uploaded for remote builds!\033[0m"
    )
    for path in samples:
        rel_path = os.path.relpath(path, start=cwd)
        logger.info(f" {rel_path}")
    if len(samples) != len(all_real_files):
        show_all = input(
            f"\033[93mShowing {len(samples)} of {len(all_real_files)}, would you like to see the rest? (y/n) \033[0m"
        )
        if show_all.lower() == "y":
            for path in all_real_files[10:]:
                rel_path = os.path.relpath(path, start=cwd)
                logger.info(f" {rel_path}")
    confirm = input("\033[1m\033[4mConfirm submitting build context? (y/n) \033[0m")
    if confirm.lower().strip() != "y":
        logger.error("Aborting!")
        sys.exit(1)

    with tempfile.TemporaryDirectory() as tempdir:
        for path in all_input_files:
            abs_path = os.path.abspath(path)

            if os.path.isdir(abs_path):
                # Recursively copy all files
                for root, _, files in os.walk(abs_path):
                    for file in files:
                        src_file = os.path.join(root, file)
                        rel_path = os.path.relpath(src_file, start=os.getcwd())
                        temp_path = os.path.join(tempdir, rel_path)
                        os.makedirs(os.path.dirname(temp_path), exist_ok=True)
                        logger.debug(f"Copying {src_file} to {temp_path}")
                        shutil.copy(src_file, temp_path)
            elif os.path.isfile(abs_path):
                rel_path = os.path.relpath(abs_path, start=os.getcwd())
                temp_path = os.path.join(tempdir, rel_path)
                os.makedirs(os.path.dirname(temp_path), exist_ok=True)
                logger.debug(f"Copying {abs_path} to {temp_path}")
                shutil.copy(abs_path, temp_path)
            else:
                logger.warning(
                    f"Path {path} does not exist or is neither file nor directory, skipping."
                )
        yield tempdir


def _build_local(image):
    """
    Build an image locally, directly with docker (for testing purposes).
    """
    with tempfile.NamedTemporaryFile() as tmp:
        tmp.write(str(image).encode())
        tmp.flush()
        tmp.seek(0)
        logger.info(f"Starting build of {tmp.name}...")
        os.execv(
            "/usr/bin/docker",
            [
                "/usr/bin/docker",
                "build",
                "-t",
                f"{image.name}:{image.tag}",
                ".",
                "-f",
                tmp.name,
            ],
        )


async def _build_remote(image, wait=None, public: bool = False, logo_id: str = None):
    """
    Build an image remotely, that is, package up the build context and ship it
    off to the chutes API to have it built.
    """
    config = get_config()
    with temporary_build_directory(image) as build_directory:
        temp_zip = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tf:
                temp_zip = tf.name
            logger.info(f"Packaging up the build directory to upload: {build_directory}")
            output_path = shutil.make_archive(temp_zip[:-4], "zip", build_directory)
            final_path = os.path.join(build_directory, "chute.zip")
            shutil.move(output_path, final_path)
        finally:
            if os.path.exists(temp_zip):
                os.remove(temp_zip)

        logger.info(f"Created the build package: {output_path}, uploading...")
        form_data = aiohttp.FormData()
        form_data.add_field("username", image.username)
        form_data.add_field("name", image.name)
        form_data.add_field("tag", image.tag)
        form_data.add_field("readme", image.readme or "")
        form_data.add_field("dockerfile", str(image))
        form_data.add_field("public", str(public))
        form_data.add_field("logo_id", str(logo_id) if logo_id else "__none__")
        form_data.add_field("wait", "False")  # unused now
        form_data.add_field("image", base64.b64encode(pickle.dumps(image)).decode())
        with open(os.path.join(build_directory, "chute.zip"), "rb") as infile:
            form_data.add_field(
                "build_context",
                BytesIO(infile.read()),
                filename="chute.zip",
                content_type="application/zip",
            )

        # Get the payload and write it to the custom writer
        payload = form_data()
        writer = FakeStreamWriter()
        await payload.write(writer)

        # Retrieve the raw bytes of the request body
        raw_data = writer.output.getvalue()

        async with aiohttp.ClientSession(base_url=config.generic.api_base_url) as session:
            headers, payload_string = sign_request(payload=raw_data)
            headers["Content-Type"] = payload.content_type
            headers["Content-Length"] = str(len(raw_data))
            async with session.post(
                "/images/",
                data=raw_data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=None),
            ) as response:
                if response.status == 409:
                    logger.error(
                        f"Image with name={image.name} and tag={image.tag} already exists!"
                    )
                elif response.status == 401:
                    logger.error("Authorization error, please check your credentials.")
                elif response.status != 202:
                    logger.error(f"Unexpected error uploading image data: {await response.text()}")
                else:
                    data = await response.json()
                    logger.info(
                        f"Uploaded image package: image_id={data['image_id']}, build will run async"
                    )

                    # Log streaming.
                    image_id = data["image_id"]
                    if wait:
                        await asyncio.sleep(5)
                        params = {}
                        attempt = 0
                        while attempt <= 10:
                            try:
                                headers, _ = sign_request(purpose="images")
                                async with session.get(
                                    f"/images/{image_id}/logs", params=params, headers=headers
                                ) as log_resp:
                                    # If we have a text/plain response, the image build is done and full logs are available.
                                    content_type = log_resp.headers.get("Content-Type", "")
                                    if "text/plain" in content_type:
                                        print(await log_resp.text())
                                        return

                                    # Error handling.
                                    if log_resp.status != 200:
                                        logger.error(
                                            f"Error streaming build logs: {await log_resp.text()}"
                                        )
                                        return

                                    # Stream the logs.
                                    async for data_enc in log_resp.content:
                                        data = data_enc.decode()
                                        if not data or not data.strip():
                                            continue
                                        if data.startswith("data: {"):
                                            data = json.loads(data[6:])
                                            log_data = data["log"]
                                            log_method = logger.info
                                            log_text = f"{log_data}"
                                            if isinstance(log_data, dict):
                                                if log_data.get("log_type") != "stdout":
                                                    log_method = logger.warning
                                                log_text = log_data.get("log", f"{log_data}")
                                            log_method(log_text.strip())
                                            params["offset"] = data["offset"]
                                        elif data.startswith("DONE"):
                                            return
                                    return
                            except Exception as exc:
                                logger.error(f"Error streaming logs, retrying...: {exc}")
                                await asyncio.sleep(1)
                                attempt += 1
                            await asyncio.sleep(5)


async def _image_exists(image: str | Image) -> bool:
    """
    Check if an image already exists.
    """
    config = get_config()
    image_id = image if isinstance(image, str) else image.uid
    logger.debug(f"Checking if {image_id=} is available...")
    headers, _ = sign_request(purpose="images")
    async with aiohttp.ClientSession(base_url=config.generic.api_base_url) as session:
        async with session.get(
            f"/images/{image_id}",
            headers=headers,
        ) as response:
            if response.status == 200:
                return True
            elif response.status == 404:
                return False
            raise Exception(await response.text())


def build_image(
    chute_ref_str: str = typer.Argument(
        ...,
        help="The chute to deploy, either a path to a chute file or a reference to a chute on the platform",
    ),
    config_path: str = typer.Option(
        None, help="Custom path to the chutes config (credentials, API URL, etc.)"
    ),
    logo: str = typer.Option(
        None,
        help="Path to the logo to use for this image.",
    ),
    local: bool = typer.Option(False, help="build the image locally, useful for testing/debugging"),
    debug: bool = typer.Option(False, help="enable debug logging"),
    include_cwd: bool = typer.Option(
        False, help="include the entire current directory in build context, recursively"
    ),
    wait: bool = typer.Option(False, help="wait for image to be built"),
    public: bool = typer.Option(False, help="mark an image as public/available to anyone"),
):
    """
    Build an image for the chutes platform.
    """

    async def _build_image():
        _, chute = load_chute(chute_ref_str=chute_ref_str, config_path=config_path, debug=debug)

        from chutes.chute import ChutePack

        # Get the image reference from the chute.
        chute = chute.chute if isinstance(chute, ChutePack) else chute
        image = chute.image

        # Pre-built?
        if isinstance(image, str):
            logger.error(
                f"You appear to be using a pre-defined/standard image '{image}', no need to build anything!"
            )
            sys.exit(1)

        # Check if the image is already built.
        if not local and await _image_exists(image):
            logger.error(f"Image with name={image.name} and tag={image.tag} already exists!")
            sys.exit(1)

        # Upload the logo, if any.
        logo_id = None
        if logo and not local:
            logo_id = await upload_logo(logo)

        # Always tack on the final directives, which include installing chutes and adding project files.
        # For remote builds, we omit installing chutes since the validator/build process injects
        # specific, locked chutes versions to allow for tracking version IDs.
        if local:
            image._directives.append(RUN("pip install chutes --upgrade"))
        current_directory = os.getcwd()
        if include_cwd:
            image._directives.append(ADD(source=".", dest="/app"))
        else:
            module_name, chute_name = chute_ref_str.split(":")
            module = importlib.import_module(module_name)
            module_path = os.path.abspath(module.__file__)
            if not module_path.startswith(current_directory):
                logger.error(
                    f"You must run the build command from the directory containing your target chute module: {module.__file__} [{current_directory=}]"
                )
                sys.exit(1)
            _clean_path = lambda in_: in_[len(current_directory) + 1 :]  # noqa: E731
            image._directives.append(
                ADD(
                    source=_clean_path(module.__file__),
                    dest=f"/app/{_clean_path(module.__file__)}",
                )
            )
        logger.debug(f"Generated Dockerfile:\n{str(image)}")

        # Building locally?
        if local:
            return _build_local(image)

        # Package up the context and ship it off for building.
        return await _build_remote(image, wait=wait, public=public, logo_id=logo_id)

    return asyncio.run(_build_image())
