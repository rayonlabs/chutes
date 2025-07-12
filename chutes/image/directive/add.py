import glob
import os
import re
from typing import List
from urllib.parse import urlparse
from chutes.image.directive import BaseDirective, DirectiveType

# Simple RE-based validation for some of the add options.
CHECKSUM_RE = re.compile(r"^(?:sha(?:256|384|512)):[a-f0-0]+$")
CHOWN_RE = re.compile(r"^([a-z_][a-z0-9_-]{0,31}|\d+)(:([a-z_][a-z0-9_-]{0,31}|\d+))?$", re.I)
CHMOD_RE = re.compile(r"^([0-7]{3})$")


class ADD(BaseDirective):
    def __init__(
        self,
        source: str,
        dest: str,
        keep_git_dir: bool = False,
        checksum: str = None,
        chown: str = None,
        chmod: str = None,
        exclude: List[str] = None,
        build_dir: str = None,
    ):
        """
        Generate a directive to add files to the image.

        :param source: Location of the source file, which can be a local file/pattern or remote URL. Uses glob path matching.
        :type source: str

        :param dest: Destination within the image.
        :type dest: str

        :param keep_git_dir: When the source is a git repository, flag indicating whether or not to also copy the .git directory.
        :type keep_git_dir: bool

        :param checksum: Optional checksum to verify remote sources upon copy.
        :type checksum: str

        :param chown: Change ownership of file to specified user upon copying.
        :type chown: str

        :param chmod: Change permissions of file upon copying.
        :type chmod: str

        :param exclude: List of path patterns to exclude (`glob.glob` path matching)
        :type exclude: List[str]

        :param build_dir: The directory we are building from/context dir, defaults to current working directory.
        :type build_dr: str

        :raises AssertionError: Validation assertions.

        :return: Directive to add data to the image.
        :rtype: ADD

        """
        self._build_context = []
        self._type = DirectiveType.ADD
        self._args = ""

        # Check if the source is a URL (including git repo).
        is_url = False
        arguments = []
        if "://" in source:
            try:
                _ = urlparse(source)
                is_url = True
            except ValueError:
                ...

        # Update remote source specific options.
        if is_url:
            if keep_git_dir:
                arguments.append("--keep-git-dir=true")
            if checksum:
                assert CHECKSUM_RE.match(checksum), f"Invalid checksum option: {checksum}"
                arguments.append(f"--checksum={checksum}")
        if chown:
            assert CHOWN_RE.match(chown), f"Invalid chown option: {chown}"
            arguments.append(f"--chown={chown}")
        if chmod:
            assert CHMOD_RE.match(chmod), f"Invalid chmod option: {chmod}"
            arguments.append(f"--chmod={chmod}")

        # Source (local/remote).
        arguments.append(source)

        # Verify source patterns if not a URL.
        if not is_url:
            positive_patterns = list(map(str, glob.glob(source, recursive=True)))
            negative_patterns = set([])
            for pattern in exclude or []:
                for path in glob.glob(pattern, recursive=True):
                    negative_patterns.add(path)
            if not build_dir:
                build_dir = os.getcwd()

            # Update the build context here, since we actually remotely sync the
            # build context and only want to include files that will actually be
            # included in the docker image.
            self._build_context = [
                path
                for path in positive_patterns
                if path not in negative_patterns and os.path.abspath(path).startswith(build_dir)
            ]
            assert self._build_context, f"No (accessible) source paths matched provided pattern '{positive_patterns}' trying to add {source}"

        # Destination within the image.
        arguments.append(dest)
        self._args = " ".join(arguments)
