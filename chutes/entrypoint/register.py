"""
Register on the chutes run platform.
"""

import asyncio
import os
import sys
import json
import glob
import time
import aiohttp
from loguru import logger
from pathlib import Path
import rich
from substrateinterface import Keypair
import typer
from chutes.config import get_generic_config
from rich import print
from chutes.constants import HOTKEY_HEADER, NONCE_HEADER, SIGNATURE_HEADER
from chutes.util.auth import get_signing_message


async def _ping_api(base_url: str):
    logger.info(f"Pinging API at {base_url}")
    try:
        async with aiohttp.ClientSession(
            base_url=base_url, timeout=aiohttp.ClientTimeout(total=2)
        ) as session:
            async with session.get("/ping") as response:
                response.raise_for_status()
                return response.status == 200
    except Exception as e:
        logger.error(
            f"Failed to connect to the API at url {base_url}: {e}. Env var 'CHUTES_API_URL' is {os.getenv('CHUTES_API_URL')}."
        )
        return False


def register(
    config_path: str = typer.Option(
        None, help="Custom path to the parachutes config (credentials, API URL, etc.)"
    ),
    username: str = typer.Option(None, help="username"),
    wallets_path: str = typer.Option(
        os.path.join(Path.home(), ".bittensor", "wallets"),
        help="path to the bittensor wallets directory",
    ),
    wallet: str = typer.Option(None, help="name of the wallet to use"),
    hotkey: str = typer.Option(None, help="hotkey to register with"),
):
    """
    Register a user!
    """

    async def _register():
        nonlocal username, wallet, hotkey
        if config_path:
            os.environ["PARACHUTES_CONFIG_PATH"] = config_path
        os.environ["PARACHUTES_ALLOW_MISSING"] = "true"
        generic_config = get_generic_config()

        from chutes.config import CONFIG_PATH

        if not await _ping_api(generic_config.api_base_url):
            sys.exit(1)

        # Interactive mode for username.
        if not username:
            username = input("Enter desired username: ").strip()
            if not username:
                logger.error("Bad choice!")
                sys.exit(1)

        # Interactive mode for wallet selection.
        if not wallet:
            available_wallets = sorted(
                [
                    os.path.basename(item)
                    for item in glob.glob(os.path.join(wallets_path, "*"))
                    if os.path.isdir(item)
                ]
            )
            print("Wallets available (commissions soon\u2122 for image/chute use):")
            for idx in range(len(available_wallets)):
                print(f"[{idx:2d}] {available_wallets[idx]}")
            choice = input("Enter your choice (number, not name): ")
            if not choice.isdigit() or not 0 <= int(choice) < len(available_wallets):
                logger.error("Bad choice!")
                sys.exit(1)
            wallet = available_wallets[int(choice)]
        else:
            if not os.path.isdir(wallet_path := os.path.join(wallets_path, wallet)):
                logger.error(f"No wallet found: {wallet_path}")
                sys.exit(1)

        # Interactive model for hotkey selection.
        if not hotkey:
            available_hotkeys = sorted(
                [
                    os.path.basename(item)
                    for item in glob.glob(
                        os.path.join(wallets_path, wallet, "hotkeys", "*")
                    )
                    if os.path.isfile(item)
                ]
            )
            print(f"Hotkeys available for {wallet}:")
            for idx in range(len(available_hotkeys)):
                print(f"[{idx:2d}] {available_hotkeys[idx]}")
            choice = input("Enter your choice (number, not name): ")
            if not choice.isdigit() or not 0 <= int(choice) < len(available_hotkeys):
                logger.error("Bad choice!")
                sys.exit(1)
            hotkey = available_hotkeys[int(choice)]
        if not os.path.isfile(
            hotkey_path := os.path.join(wallets_path, wallet, "hotkeys", hotkey)
        ):
            logger.error(f"No hotkey found: {hotkey_path}")
            sys.exit(1)

        rich.print(
            f"\nAttempting to register the user {username} with the wallet located at {os.path.join(wallets_path, wallet, 'hotkeys', hotkey)}.\n"
        )

        # Send it.
        with open(hotkey_path) as infile:
            hotkey_data = json.load(infile)
        with open(os.path.join(wallets_path, wallet, "coldkeypub.txt")) as infile:
            coldkey_pub_data = json.load(infile)
        ss58 = hotkey_data["ss58Address"]
        secret_seed = hotkey_data["secretSeed"].replace("0x", "")
        coldkey_ss58 = coldkey_pub_data["ss58Address"]
        payload = json.dumps(
            {
                "username": username,
                "coldkey": coldkey_ss58,
            }
        )
        keypair = Keypair.create_from_seed(seed_hex=secret_seed)
        headers = {
            "Content-Type": "application/json",
            HOTKEY_HEADER: ss58,
            NONCE_HEADER: str(int(time.time())),
        }
        sig_str = get_signing_message(ss58, headers[NONCE_HEADER], payload)
        headers[SIGNATURE_HEADER] = keypair.sign(sig_str.encode()).hex()
        logger.debug(
            f"Sending payload: {payload} with headers: {headers}. Signing message was :"
        )
        async with aiohttp.ClientSession(
            base_url=generic_config.api_base_url
        ) as session:
            async with session.post(
                "/users/register",
                data=payload,
                headers=headers,
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    logger.success(
                        f"User created successfully: user_id={data['user_id']}, updated config.ini:"
                    )
                    updated_config = "\n".join(
                        [
                            "[api]",
                            f"base_url = {generic_config.api_base_url}",
                            "",
                            "[auth]",
                            f"user_id = {data['user_id']}",
                            f"hotkey_seed = {secret_seed}",
                            f"hotkey_name = {hotkey}",
                            f"hotkey_ss58address = {ss58}",
                            "",
                            "[payment]",
                            f"address = {data['payment_address']}",
                        ]
                    )
                    print(updated_config + "\n\n")
                    save = input(f"Save to {CONFIG_PATH} (y/n): ")
                    if save.strip().lower() == "y":
                        with open(CONFIG_PATH, "w") as outfile:
                            outfile.write(updated_config + "\n")
                    logger.success(
                        f"Successfully registered username={data['username']}, with fingerprint {data['fingerprint']}. "
                        "Keep the fingerprint safe as this is account login credentials - do not lose or share it!"
                        "To add balance for your account, send tao to {data['payment_address']}."
                    )
                else:
                    logger.error(await response.json())

    asyncio.run(_register())
