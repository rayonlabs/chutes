import os
import sys
import stat
import subprocess
from pathlib import Path


def main():
    binary_path = Path(__file__).parent / "cfsv"
    os.chmod(binary_path, os.stat(binary_path).st_mode | stat.S_IEXEC)
    result = subprocess.run([str(binary_path)] + sys.argv[1:])
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
