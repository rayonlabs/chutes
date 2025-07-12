"""
Run a chute, automatically handling encryption/decryption via GraVal.
"""

import os
import re
import asyncio
import aiohttp
import sys
import jwt
import time
import uuid
import inspect
import typer
import psutil
import base64
import secrets
import orjson as json
from loguru import logger
from typing import Optional, Any
from datetime import datetime
from pydantic import BaseModel
from ipaddress import ip_address
from uvicorn import Config, Server
from fastapi import FastAPI, Request, Response, status, HTTPException
from fastapi.responses import ORJSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from substrateinterface import Keypair, KeypairType
from chutes.entrypoint._shared import load_chute, miner, authenticate_request
from chutes.entrypoint.ssh import setup_ssh_access
from chutes.chute import ChutePack, Job
from chutes.util.context import is_local
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding


def get_all_process_info():
    """
    Return running process info.
    """
    processes = {}
    for proc in psutil.process_iter(["pid", "name", "cmdline", "open_files", "create_time"]):
        try:
            info = proc.info
            info["open_files"] = [f.path for f in proc.open_files()]
            info["create_time"] = datetime.fromtimestamp(proc.create_time()).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            info["environ"] = dict(proc.environ())
            processes[str(proc.pid)] = info
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return Response(
        content=json.dumps(processes).decode(),
        media_type="application/json",
    )


def get_env_sig(request: Request):
    """
    Environment signature check.
    """
    import chutes.envcheck as envcheck

    return Response(
        content=envcheck.signature(request.state.decrypted["salt"]),
        media_type="text/plain",
    )


def get_env_dump(request: Request):
    """
    Base level environment check, running processes and things.
    """
    import chutes.envcheck as envcheck

    key = bytes.fromhex(request.state.decrypted["key"])
    return Response(
        content=envcheck.dump(key),
        media_type="text/plain",
    )


async def get_metrics():
    """
    Get the latest prometheus metrics.
    """
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def get_devices():
    """
    Fetch device information.
    """
    return [miner().get_device_info(idx) for idx in range(miner()._device_count)]


async def process_device_challenge(request: Request, challenge: str):
    """
    Process a GraVal device info challenge string.
    """
    return Response(
        content=miner().process_device_info_challenge(challenge),
        media_type="text/plain",
    )


async def process_fs_challenge(request: Request):
    """
    Process a filesystem challenge.
    """
    challenge = FSChallenge(**request.state.decrypted)
    return Response(
        content=miner().process_filesystem_challenge(
            filename=challenge.filename,
            offset=challenge.offset,
            length=challenge.length,
        ),
        media_type="text/plain",
    )


async def handle_slurp(request: Request, chute_module):
    """
    Read part or all of a file.
    """
    slurp = Slurp(**request.state.decrypted)
    if slurp.path == "__file__":
        source_code = inspect.getsource(chute_module)
        return Response(
            content=base64.b64encode(source_code.encode()).decode(),
            media_type="text/plain",
        )
    elif slurp.path == "__run__":
        source_code = inspect.getsource(sys.modules[__name__])
        return Response(
            content=base64.b64encode(source_code.encode()).decode(),
            media_type="text/plain",
        )
    if not os.path.isfile(slurp.path):
        if os.path.isdir(slurp.path):
            if hasattr(request.state, "_encrypt"):
                return {"json": request.state._encrypt(json.dumps({"dir": os.listdir(slurp.path)}))}
            return {"dir": os.listdir(slurp.path)}
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Path not found: {slurp.path}",
        )
    response_bytes = None
    with open(slurp.path, "rb") as f:
        f.seek(slurp.start_byte)
        if slurp.end_byte is None:
            response_bytes = f.read()
        else:
            response_bytes = f.read(slurp.end_byte - slurp.start_byte)
    response_data = {"contents": base64.b64encode(response_bytes).decode()}
    if hasattr(request.state, "_encrypt"):
        return {"json": request.state._encrypt(json.dumps(response_data))}
    return response_data


async def pong(request: Request) -> dict[str, Any]:
    """
    Echo incoming request as a liveness check.
    """
    if hasattr(request.state, "_encrypt"):
        return {"json": request.state._encrypt(json.dumps(request.state.decrypted))}
    return request.state.decrypted


async def get_token(request: Request) -> dict[str, Any]:
    """
    Fetch a token, useful in detecting proxies between the real deployment and API.
    """
    endpoint = request.state.decrypted.get(
        "endpoint", "https://api.chutes.ai/instances/token_check"
    )
    salt = request.state.decrypted.get("salt", 42)
    async with aiohttp.ClientSession(trust_env=True) as session:
        async with session.get(endpoint, params={"salt": salt}) as resp:
            if hasattr(request.state, "_encrypt"):
                return {"json": request.state._encrypt(await resp.text())}
            return await resp.json()


async def is_alive(request: Request):
    """
    Liveness probe endpoint for k8s.
    """
    return {"alive": True}


class Slurp(BaseModel):
    path: str
    start_byte: Optional[int] = 0
    end_byte: Optional[int] = None


class FSChallenge(BaseModel):
    filename: str
    length: int
    offset: int


class DevMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        """
        Dev/dummy dispatch.
        """
        args = await request.json() if request.method in ("POST", "PUT", "PATCH") else None
        request.state.serialized = False
        request.state.decrypted = args
        return await call_next(request)


class GraValMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI, concurrency: int = 1, symmetric_key: str = None):
        """
        Initialize a semaphore for concurrency control/limits.
        """
        super().__init__(app)
        self.concurrency = concurrency
        self.lock = asyncio.Lock()
        self.requests_in_flight = {}
        self.symmetric_key = symmetric_key
        self.app = app

    async def _dispatch(self, request: Request, call_next):
        """
        Transparently handle decryption and verification.
        """
        if request.client.host == "127.0.0.1":
            return await call_next(request)

        # Internal endpoints.
        path = request.scope.get("path", "")
        if path.endswith(("/_alive", "/_metrics")):
            ip = ip_address(request.client.host)
            is_private = (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            )
            if not is_private:
                return ORJSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": "go away (internal)"},
                )
            else:
                return await call_next(request)

        # Authentication...
        body_bytes, failure_response = await authenticate_request(request, miner())
        if failure_response:
            return failure_response

        # Decrypt using the symmetric key we exchanged via GraVal.
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                iv = bytes.fromhex(body_bytes[:32].decode())
                cipher = Cipher(
                    algorithms.AES(self.symmetric_key),
                    modes.CBC(iv),
                    backend=default_backend(),
                )
                unpadder = padding.PKCS7(128).unpadder()
                decryptor = cipher.decryptor()
                decrypted_data = (
                    decryptor.update(base64.b64decode(body_bytes[32:])) + decryptor.finalize()
                )
                unpadded_data = unpadder.update(decrypted_data) + unpadder.finalize()
                try:
                    request.state.decrypted = json.loads(unpadded_data)
                except Exception:
                    request.state.decrypted = json.loads(unpadded_data.rstrip(bytes(range(1, 17))))
                request.state.iv = iv
            except ValueError as exc:
                return ORJSONResponse(
                    status_code=status.HTTP_451_UNAVAILABLE_FOR_LEGAL_REASONS,
                    content={"detail": f"Decryption failed: {exc}"},
                )

            def _encrypt(plaintext: bytes):
                if isinstance(plaintext, str):
                    plaintext = plaintext.encode()
                padder = padding.PKCS7(128).padder()
                cipher = Cipher(
                    algorithms.AES(self.symmetric_key),
                    modes.CBC(iv),
                    backend=default_backend(),
                )
                padded_data = padder.update(plaintext) + padder.finalize()
                encryptor = cipher.encryptor()
                encrypted_data = encryptor.update(padded_data) + encryptor.finalize()
                return base64.b64encode(encrypted_data).decode()

            request.state._encrypt = _encrypt

        return await call_next(request)

    async def dispatch(self, request: Request, call_next):
        """
        Rate-limiting wrapper around the actual dispatch function.
        """
        request.request_id = str(uuid.uuid4())
        request.state.serialized = request.headers.get("X-Chutes-Serialized") is not None

        # Pass regular, special paths through.
        if (
            request.scope.get("path", "").endswith(
                (
                    "/_fs_challenge",
                    "/_alive",
                    "/_metrics",
                    "/_ping",
                    "/_procs",
                    "/_slurp",
                    "/_device_challenge",
                    "/_devices",
                    "/_env_sig",
                    "/_env_dump",
                    "/_token",
                    "/_dump",
                    "/_sig",
                    "/_toca",
                    "/_eslurp",
                )
            )
            or request.client.host == "127.0.0.1"
        ):
            return await self._dispatch(request, call_next)

        # Decrypt encrypted paths, which could be one of the above as well.
        path = request.scope.get("path", "")
        try:
            iv = bytes.fromhex(path[1:33])
            cipher = Cipher(
                algorithms.AES(self.symmetric_key),
                modes.CBC(iv),
                backend=default_backend(),
            )
            unpadder = padding.PKCS7(128).unpadder()
            decryptor = cipher.decryptor()
            decrypted_data = decryptor.update(bytes.fromhex(path[33:])) + decryptor.finalize()
            actual_path = unpadder.update(decrypted_data) + unpadder.finalize()
            actual_path = actual_path.decode().rstrip("?")
            logger.info(f"Decrypted request path: {actual_path} from input path: {path}")
            request.scope["path"] = actual_path
        except ValueError:
            return ORJSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"detail": f"Bad path: {path}"},
            )

        # Now pass the decrypted special paths through.
        if request.scope.get("path", "").endswith(
            (
                "/_fs_challenge",
                "/_alive",
                "/_metrics",
                "/_ping",
                "/_procs",
                "/_slurp",
                "/_device_challenge",
                "/_devices",
                "/_env_sig",
                "/_env_dump",
                "/_token",
                "/_dump",
                "/_sig",
                "/_toca",
                "/_eslurp",
            )
        ):
            return await self._dispatch(request, call_next)

        # Concurrency control with timeouts in case it didn't get cleaned up properly.
        async with self.lock:
            now = time.time()
            if len(self.requests_in_flight) >= self.concurrency:
                purge_keys = []
                for key, val in self.requests_in_flight.items():
                    if now - val >= 600:
                        logger.warning(
                            f"Assuming this request is no longer in flight, killing: {key}"
                        )
                        purge_keys.append(key)
                if purge_keys:
                    for key in purge_keys:
                        self.requests_in_flight.pop(key, None)
                    self.requests_in_flight[request.request_id] = now
                else:
                    return ORJSONResponse(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        content={
                            "error": "RateLimitExceeded",
                            "detail": f"Max concurrency exceeded: {self.concurrency}, try again later.",
                        },
                    )
            else:
                self.requests_in_flight[request.request_id] = now

        # Perform the actual request.
        response = None
        try:
            response = await self._dispatch(request, call_next)
            if hasattr(response, "body_iterator"):
                original_iterator = response.body_iterator

                async def wrapped_iterator():
                    try:
                        async for chunk in original_iterator:
                            yield chunk
                    except Exception as exc:
                        logger.warning(f"Unhandled exception in body iterator: {exc}")
                        self.requests_in_flight.pop(request.request_id, None)
                        raise
                    finally:
                        self.requests_in_flight.pop(request.request_id, None)

                response.body_iterator = wrapped_iterator()
                return response
            return response
        finally:
            if not response or not hasattr(response, "body_iterator"):
                self.requests_in_flight.pop(request.request_id, None)


async def _gather_devices_and_initialize(
    token: str, host: str, port_mappings: list[dict[str, Any]]
) -> dict:
    """
    Gather the GPU info assigned to this pod, submit with our one-time token to get GraVal seed.
    """
    from chutes.envdump import DUMPER

    # Build the GraVal request based on the GPUs that were actually assigned to this pod.
    logger.info("Collecting GPUs and port mappings...")
    body = {"gpus": [], "port_mappings": port_mappings, "host": host}
    for idx in range(miner()._device_count):
        body["gpus"].append(miner().get_device_info(idx))
    token_data = jwt.decode(token, options={"verify_signature": False})
    url = token_data.get("url")
    key = token_data.get("env_key", "a" * 32)

    logger.info("Collecting full envdump...")
    body["env"] = DUMPER.dump(key)

    # Fetch the challenges.
    async with aiohttp.ClientSession(raise_for_status=True) as session:
        logger.info(f"Collected all environment data, submitting to validator: {url}")
        async with session.post(url, headers={"Authorization": token}, json=body) as resp:
            init_params = await resp.json()
            logger.success(f"Successfully fetched initialization params: {init_params=}")

            # First, we initialize graval on all GPUs from the provided seed.
            miner()._graval_seed = init_params["seed"]
            iterations = init_params.get("iterations", 1)
            logger.info(f"Generating proofs from seed={miner()._graval_seed}")
            proofs = miner().prove(miner()._graval_seed, iterations=iterations)

            # Run filesystem verification challenge.
            seed_str = str(init_params["seed"])
            fsv_hash = None
            try:
                logger.info(f"Running filesystem verification challenge with seed={seed_str}")
                cfsv_path = os.path.join(os.path.dirname(__file__), "..", "cfsv")
                result = subprocess.run(
                    ["cfsv", "challenge", "/", seed_str, "full", "/etc/chutesfs.index"],
                    capture_output=True,
                    text=True,
                    check=True
                )
                for line in result.stdout.strip().split('\n'):
                    if line.startswith("RESULT:"):
                        fsv_hash = line.split("RESULT:")[1].strip()
                        logger.success(f"Filesystem verification hash: {fsv_hash}")
                        break
                if not fsv_hash:
                    logger.warning("Failed to extract filesystem verification hash from cfsv output")
            except subprocess.CalledProcessError as e:
                logger.error(f"cfsv challenge failed: {e.stderr}")
            except Exception as e:
                logger.error(f"Error running cfsv challenge: {e}")
            if not fsv_hash:
                raise Exception("Failed to generate filesystem challenge response.")

            # Use GraVal to extract the symmetric key from the challenge.
            sym_key = init_params["symmetric_key"]
            bytes_ = base64.b64decode(sym_key["ciphertext"])
            iv = bytes_[:16]
            cipher = bytes_[16:]
            logger.info("Decrypting payload via proof challenge matrix...")
            symmetric_key = bytes.fromhex(
                miner().decrypt(
                    init_params["seed"], cipher, iv, len(cipher), sym_key["device_index"]
                )
            )

            # Now, we can respond to the URL by encrypting a payload with the symmetric key and sending it back.
            padder = padding.PKCS7(128).padder()
            new_iv = secrets.token_bytes(16)
            cipher = Cipher(
                algorithms.AES(symmetric_key),
                modes.CBC(new_iv),
                backend=default_backend(),
            )
            plaintext = sym_key["response_plaintext"]
            padded_data = padder.update(plaintext.encode()) + padder.finalize()
            encryptor = cipher.encryptor()
            encrypted_data = encryptor.update(padded_data) + encryptor.finalize()
            response_cipher = base64.b64encode(encrypted_data).decode()
            logger.success(
                f"Completed PoVW challenge, sending back: {plaintext=} "
                f"as {response_cipher=} where iv={new_iv.hex()}"
            )

            # Post the response to the challenge, which returns job data (if any).
            async with session.put(
                url,
                headers={"Authorization": token},
                json={
                    "response": response_cipher,
                    "iv": new_iv.hex(),
                    "proof": proofs,
                    "fsv": fsv_hash,
                },
            ) as resp:
                logger.success("Successfully negotiated challenge response!")
                return symmetric_key, await resp.json()


# Run a chute (which can be an async job or otherwise long-running process).
def run_chute(
    chute_ref_str: str = typer.Argument(
        ..., help="chute to run, in the form [module]:[app_name], similar to uvicorn"
    ),
    miner_ss58: str = typer.Option(None, help="miner hotkey ss58 address"),
    validator_ss58: str = typer.Option(None, help="validator hotkey ss58 address"),
    host: str | None = typer.Option("0.0.0.0", help="host to bind to"),
    port: int | None = typer.Option(8000, help="port to listen on"),
    logging_port: int | None = typer.Option(8001, help="logging port"),
    keyfile: str | None = typer.Option(None, help="path to TLS key file"),
    certfile: str | None = typer.Option(None, help="path to TLS certificate file"),
    debug: bool = typer.Option(False, help="enable debug logging"),
    dev: bool = typer.Option(False, help="dev/local mode"),
    dev_job_data_path: str = typer.Option(None, help="dev mode: job payload JSON path"),
    dev_job_method: str = typer.Option(None, help="dev mode: job method"),
):
    async def _run_chute():
        """
        Run the chute (or job).
        """
        # Load the chute.
        chute_module, chute = load_chute(chute_ref_str=chute_ref_str, config_path=None, debug=debug)
        if is_local():
            logger.error("Cannot run chutes in local context!")
            sys.exit(1)

        chute = chute.chute if isinstance(chute, ChutePack) else chute

        # Load token and port mappings from the environment.
        token = os.getenv("CHUTES_LAUNCH_JWT")
        port_mappings = [
            # Main chute pod.
            {
                "proto": "tcp",
                "internal_port": port,
                "external_port": port,
            },
            # Logging server.
            {
                "proto": "tcp",
                "internal_port": logging_port,
                "external_port": logging_port,
            },
        ]
        external_host = os.getenv("CHUTES_EXTERNAL_HOST")
        primary_port = os.getenv("CHUTES_PORT_PRIMARY")
        if primary_port and primary_port.isdigit():
            port_mappings[0]["external_port"] = int(primary_port)
        ext_logging_port = os.getenv("CHUTES_PORT_LOGGING")
        if ext_logging_port and ext_logging_port.isdigit():
            port_mappings[1]["external_port"] = int(ext_logging_port)
        for key, value in os.environ.items():
            port_match = re.match(r"^CHUTES_PORT_(TCP|UDP|HTTP)_[0-9]+", key)
            if port_match and value.isdigit():
                port_mappings.append(
                    {
                        "proto": port_match.group(1),
                        "internal_port": int(port_match.group(2)),
                        "external_port": int(value),
                    }
                )

        # GPU verification plus job fetching.
        job_data: dict | None = None
        symmetric_key: str | None = None
        job_id: str | None = None
        job_obj: Job | None = None
        job_method: str | None = None
        job_status_url: str | None = None
        if token:
            symmetric_key, response = await _gather_devices_and_initialize(
                token, external_host, port_mappings
            )
            job_id = response.get("job_id")
            job_method = response.get("job_method")
            job_status_url = response.get("job_status_url")
            if job_method:
                job_obj = next(j for j in chute._jobs if j.name == job_method)
            job_data = response.get("job_data")

        elif not dev:
            logger.error("No GraVal token supplied!")
            sys.exit(1)

        # Configure dev method job payload/method/etc.
        if dev and dev_job_data_path:
            with open(dev_job_data_path) as infile:
                job_data = json.loads(infile.read())
            job_id = str(uuid.uuid4())
            job_method = dev_job_method
            job_obj = next(j for j in chute._jobs if j.name == dev_job_method)
            logger.info(f"Creating task, dev mode, for {job_method=}")

        # Run the chute's initialization code.
        await chute.initialize()

        # Encryption/rate-limiting middleware setup.
        if dev:
            chute.add_middleware(DevMiddleware)
        else:
            chute.add_middleware(
                GraValMiddleware,
                concurrency=chute.concurrency,
                symmetric_key=symmetric_key,
            )

        # Slurps and processes.
        async def _handle_slurp(request: Request):
            nonlocal chute_module

            return await handle_slurp(request, chute_module)

        # Validation endpoints.
        chute.add_api_route("/_ping", pong, methods=["POST"])
        chute.add_api_route("/_token", get_token, methods=["POST"])
        chute.add_api_route("/_alive", is_alive, methods=["GET"])
        chute.add_api_route("/_metrics", get_metrics, methods=["GET"])
        chute.add_api_route("/_slurp", _handle_slurp, methods=["POST"])
        chute.add_api_route("/_procs", get_all_process_info, methods=["GET"])
        chute.add_api_route("/_env_sig", get_env_sig, methods=["POST"])
        chute.add_api_route("/_env_dump", get_env_dump, methods=["POST"])
        chute.add_api_route("/_devices", get_devices, methods=["GET"])
        chute.add_api_route("/_device_challenge", process_device_challenge, methods=["GET"])
        chute.add_api_route("/_fs_challenge", process_fs_challenge, methods=["POST"])

        # New envdump endpoints.
        import chutes.envdump as envdump
        chute.add_api_route("/_dump", envdump.handle_dump, methods=["POST"])
        chute.add_api_route("/_sig", envdump.handle_sig, methods=["POST"])
        chute.add_api_route("/_toca", envdump.handle_toca, methods=["POST"])
        chute.add_api_route("/_eslurp", envdump.handle_slurp, methods=["POST"])

        logger.success("Added all chutes internal endpoints.")

        # Job shutdown/kill endpoint.
        async def _shutdown():
            nonlocal job_obj, server
            if not job_obj:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Job task not found",
                )
            logger.warning("Shutdown requested.")
            if job_obj and not job_obj.cancel_event.is_set():
                job_obj.cancel_event.set()
            server.should_exit = True
            return {"ok": True}

        # Jobs can't be started until the full suite of validation tests run,
        # so we need to provide an endpoint for the validator to use to kick
        # it off.
        if job_id:
            job_task = None

            async def start_job_with_monitoring(**kwargs):
                nonlocal job_task
                ssh_process = None
                job_task = asyncio.create_task(job_obj.run(job_status_url=job_status_url, **kwargs))

                async def monitor_job():
                    try:
                        result = await job_task
                        logger.info(f"Job completed with result: {result}")
                    except Exception as e:
                        logger.error(f"Job failed with error: {e}")
                    finally:
                        logger.info("Job finished, shutting down server...")
                        if ssh_process:
                            try:
                                ssh_process.terminate()
                                await asyncio.sleep(0.5)
                                if ssh_process.poll() is None:
                                    ssh_process.kill()
                                logger.info("SSH server stopped")
                            except Exception as e:
                                logger.error(f"Error stopping SSH server: {e}")
                        server.should_exit = True

                # If the pod defines SSH access, enable it.
                if job_obj.ssh and job_data.get("_ssh_public_key"):
                    ssh_process = await setup_ssh_access(job_data["_ssh_public_key"])

                asyncio.create_task(monitor_job())

            await start_job_with_monitoring(**job_data)
            logger.info("Started job!")

            chute.add_api_route("/_shutdown", _shutdown, methods=["POST"])
            logger.info("Added shutdown endpoint")

        # Start the uvicorn process, whether in job mode or not.
        config = Config(
            app=chute,
            host=host or "0.0.0.0",
            port=port or 8000,
            limit_concurrency=1000,
            ssl_certfile=certfile,
            ssl_keyfile=keyfile,
        )
        server = Server(config)
        await server.serve()

    # Kick everything off
    async def _logged_run():
        """
        Wrap the actual chute execution with the logging process, which is
        kept alive briefly after the main process terminates.
        """
        from chutes.entrypoint.logger import launch_server

        if not dev:
            miner()._miner_ss58 = miner_ss58
            miner()._validator_ss58 = validator_ss58
            miner()._keypair = Keypair(ss58_address=validator_ss58, crypto_type=KeypairType.SR25519)

        logging_task = asyncio.create_task(
            launch_server(
                host=host or "0.0.0.0",
                port=logging_port,
                dev=dev,
                certfile=certfile,
                keyfile=keyfile,
            )
        )
        try:
            await _run_chute()
        finally:
            await asyncio.sleep(30)
            logging_task.cancel()
            try:
                await logging_task
            except asyncio.CancelledError:
                pass

    asyncio.run(_logged_run())
