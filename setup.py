# NOTE: Should replace with a pyproject.toml
import os
import sys
from setuptools import setup, find_packages

# Load full readme.
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "README.md")) as infile:
    long_description = infile.read()

# Load version.
here = os.path.abspath(os.path.dirname(__file__))
sys.path.append(os.path.join(here, "chutes"))
import _version  # noqa

version = _version.version

setup(
    name="chutes",
    version=version,
    description="Chutes development kit and CLI.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/rayonlabs/chutes",
    author="Jon Durbin",
    license_expression="MIT",
    packages=find_packages(),
    include_package_data=True,
    package_data={
        "chutes": [
            "chutes/pyarmor_runtime_006563/*",
            "chutes/pyarmor_runtime_006563/**/*",
            "chutes/envcheck/*.py",
            "*.so",
            "cfsv",
        ],
        "chutes.envdump": ["*.so"],
    },
    install_requires=[
        "aiohttp[speedups]>=3.10,<4",
        "backoff>=2.2,<3",
        "requests>=2.32",
        "loguru==0.7.2",
        "fastapi>=0.115",
        "uvicorn>=0.32.0",
        "pydantic>=2.9,<3",
        "orjson>=3.10",
        "fickling==0.1.3",
        "setuptools>=0.75",
        "substrate-interface>=1.7.11",
        "rich>=13.0.0",
        "typer>=0.12.5",
        "graval>=0.1.2",
        "prometheus-client==0.21.0",
        "cryptography",
        "psutil",
        "pyjwt>=2.10.1",
        "netifaces",
        "pyudev",
        "aiofiles>=23",
    ],
    extras_require={
        "dev": [
            "black",
            "flake8",
            "wheel",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3.10",
    ],
    entry_points={
        "console_scripts": [
            "chutes=chutes.cli:app",
            "cfsv=chutes.cfsv_wrapper:main",
        ],
    },
)
