import os
import pwd
import subprocess
from loguru import logger

SSHD_CONFIG = """
Port 2202
ListenAddress 0.0.0.0
HostKey /tmp/ssh_host_rsa_key
HostKey /tmp/ssh_host_ed25519_key
PubkeyAuthentication yes
PasswordAuthentication no
ChallengeResponseAuthentication no
UsePAM no
X11Forwarding no
PrintMotd no
AcceptEnv LANG LC_*
Subsystem sftp /usr/lib/openssh/sftp-server
PidFile /tmp/sshd.pid
"""
ROOT_SSHD_CONFIG = f"{SSHD_CONFIG}\nPermitRootLogin prohibit-password"


async def setup_ssh_access(ssh_public_key):
    """
    Setup SSH access with the provided public key for current user.
    """
    try:
        uid = os.getuid()
        user_info = pwd.getpwuid(uid)
        username = user_info.pw_name
        home_dir = user_info.pw_dir
        logger.info(f"Setting up SSH access for user: {username} (uid: {uid})")
        ssh_dir = os.path.join(home_dir, ".ssh")
        os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
        authorized_keys_path = os.path.join(ssh_dir, "authorized_keys")
        with open(authorized_keys_path, "a") as f:
            f.write(f"{ssh_public_key}\n")
        os.chmod(authorized_keys_path, 0o600)
        os.chown(ssh_dir, uid, -1)
        os.chown(authorized_keys_path, uid, -1)
        sshd_config_content = SSHD_CONFIG if uid != 0 else ROOT_SSHD_CONFIG
        sshd_config_path = "/tmp/sshd_config_minimal"
        with open(sshd_config_path, "w") as f:
            f.write(sshd_config_content)
        if not os.path.exists("/tmp/ssh_host_rsa_key"):
            subprocess.run(
                ["ssh-keygen", "-t", "rsa", "-f", "/tmp/ssh_host_rsa_key", "-N", ""], check=True
            )
        if not os.path.exists("/tmp/ssh_host_ed25519_key"):
            subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-f", "/tmp/ssh_host_ed25519_key", "-N", ""],
                check=True,
            )
        subprocess.Popen(["/usr/sbin/sshd", "-D", "-f", sshd_config_path])
        logger.info(f"SSH server started successfully on port 2202 for user {username}")

    except Exception as e:
        logger.error(f"Failed to setup SSH access: {e}")
