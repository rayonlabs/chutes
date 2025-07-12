#!/usr/bin/env python

import os
import sys
import glob
import typer
from loguru import logger
from pathlib import Path
from chutes.entrypoint.api_key import create_api_key
from chutes.entrypoint.deploy import deploy_chute
from chutes.entrypoint.register import register
from chutes.entrypoint.build import build_image
from chutes.entrypoint.report import report_invocation
from chutes.entrypoint.run import run_chute
from chutes.entrypoint.link import link_hotkey
from chutes.entrypoint.fingerprint import change_fingerprint
from chutes.crud import chutes_app, images_app, api_keys_app

app = typer.Typer(no_args_is_help=True)

# Inject the logging intercept library.
if len(sys.argv) > 1 and sys.argv[1] == "run" and "CHUTE_LD_PRELOAD_INJECTED" not in os.environ:
    logger_lib = Path(__file__).parent / "chutes-logintercept.so"
    if os.path.exists(logger_lib):
        env = os.environ.copy()
        env["LD_PRELOAD"] = logger_lib
        env["CHUTE_LD_PRELOAD_INJECTED"] = "1"
        [os.remove(f) for f in glob.glob("/tmp/_chute*log*")]
        os.execve(sys.executable, [sys.executable] + sys.argv, env)
    else:
        logger.warning("Chutes log intercept lib not found, proceeding with standard logging")

app.command(name="register", help="Create an account with the chutes run platform!")(register)
app.command(
    name="link",
    help="Link a validator or subnet owner hotkey to your account, which grants free+developer access!",
)(link_hotkey)
app.command(help="Change your fingerprint!", no_args_is_help=True, name="refinger")(
    change_fingerprint
)
app.command(help="Report an invocation!", no_args_is_help=True, name="report")(report_invocation)
app.command(help="Run a chute!", no_args_is_help=True, name="run")(run_chute)
app.command(help="Deploy a chute!", no_args_is_help=True, name="deploy")(deploy_chute)
app.command(help="Build an image!", no_args_is_help=True, name="build")(build_image)

# Chutes
app.add_typer(chutes_app, name="chutes")

# Images
app.add_typer(images_app, name="images")

# API Keys
api_keys_app.command(
    help="Create an API key for the chutes run platform!",
    no_args_is_help=True,
    name="create",
)(create_api_key)
app.add_typer(api_keys_app)

if __name__ == "__main__":
    app()
