#!/usr/bin/env python3
#
# Copyright (C) 2025 KeePassXC Team <team@keepassxc.org>
#
# This program is free software: you can redistribute it and/or modify
# it # under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 or (at your option)
# version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


import argparse
import ctypes
import signal
from datetime import datetime
import logging
import os
from pathlib import Path
import platform
import re
import signal
import shutil
import subprocess
import sys


###########################################################################################
#                                       Globals
###########################################################################################


RELEASE_NAME = None
APP_NAME = 'KeePassXC'
SRC_DIR = os.getcwd()
GPG_KEY = 'BF5A669F2272CF4324C1FDA8CFB4C2166397D0D2'
GPG_GIT_KEY = None
OUTPUT_DIR = 'release'
ORIG_GIT_BRANCH = None
SOURCE_BRANCH = None
TAG_NAME = None
DOCKER_IMAGE = None
DOCKER_CONTAINER_NAME = 'keepassxc-build-container'
CMAKE_OPTIONS = []
CPACK_GENERATORS = 'ZIP'
COMPILER = 'g++'
MAKE_OPTIONS = f'-j{os.cpu_count()}'
BUILD_PLUGINS = 'all'
INSTALL_PREFIX = '/usr/local'
MACOSX_DEPLOYMENT_TARGET = '12'
TIMESTAMP_SERVER = 'http://timestamp.sectigo.com'


###########################################################################################
#                                   Errors and Logging
###########################################################################################


class Error(Exception):
    def __init__(self, msg, *args, **kwargs):
        self.msg = msg
        self.args = args
        self.kwargs = kwargs
        self.__dict__.update(kwargs)


class SubprocessError(Error):
    pass


class LogFormatter(logging.Formatter):
    _CLR = 'color' in os.getenv('TERM', '') or 'CLICOLOR_FORCE' in os.environ or sys.platform == 'win32'

    BOLD = '\x1b[1m' if _CLR else ''
    RED = '\x1b[31m' if _CLR else ''
    BRIGHT_RED = '\x1b[91m' if _CLR else ''
    YELLOW = '\x1b[33m' if _CLR else ''
    BLUE = '\x1b[34m' if _CLR else ''
    GREEN = '\x1b[32m' if _CLR else ''
    END = '\x1b[0m' if _CLR else ''

    _FMT = {
        logging.DEBUG: f'{BOLD}[%(levelname)s{END}{BOLD}]{END} %(message)s',
        logging.INFO: f'{BOLD}[{BLUE}%(levelname)s{END}{BOLD}]{END} %(message)s',
        logging.WARNING: f'{BOLD}[{YELLOW}%(levelname)s{END}{BOLD}]{END}{YELLOW} %(message)s{END}',
        logging.ERROR: f'{BOLD}[{RED}%(levelname)s{END}{BOLD}]{END}{RED} %(message)s{END}',
        logging.CRITICAL: f'{BOLD}[{BRIGHT_RED}%(levelname)s{END}{BOLD}]{END}{BRIGHT_RED} %(message)s{END}',
    }

    def format(self, record):
        return logging.Formatter(self._FMT.get(record.levelno)).format(record)


console_handler = logging.StreamHandler()
console_handler.setFormatter(LogFormatter())
logger = logging.getLogger(__file__)
logger.setLevel(os.getenv('LOGLEVEL') if 'LOGLEVEL' in os.environ else logging.INFO)
logger.addHandler(console_handler)


###########################################################################################
#                                      Helper Functions
###########################################################################################


def _get_bin_path(build_dir):
    if not build_dir:
        return None
    build_dir = Path(build_dir)
    path_sep = ';' if sys.platform == 'win32' else ':'
    return path_sep.join(list(map(str, build_dir.rglob('vcpkg_installed/*/tools/**/bin'))) + [os.getenv('PATH')])


def _run(cmd, *args, path=None, env=None, input=None, capture_output=True, cwd=None,
         timeout=None, check=True, **kwargs):
    """
    Run a command and return its output.
    Raises an error if ``check`` is ``True`` and the process exited with a non-zero code.
    """
    if not cmd:
        raise ValueError('Empty command given.')

    if not env:
        env = {}
    if path:
        env['PATH'] = path

    try:
        return subprocess.run(
            cmd, *args,
            input=input,
            capture_output=capture_output,
            cwd=cwd,
            env=env,
            timeout=timeout,
            check=check,
            **kwargs)
    except FileNotFoundError:
        raise Error('Command not found: %s', cmd[0] if type(cmd) in [list, tuple] else cmd)
    except subprocess.CalledProcessError as e:
        if e.stderr:
            raise SubprocessError('Command "%s" exited with non-zero code. Error: %s',
                                  cmd[0], e.stderr, **e.__dict__)
        else:
            raise SubprocessError('Command "%s" exited with non-zero code.', cmd[0], **e.__dict__)


def _cmd_exists(cmd, path=None):
    """Check if command exists."""
    return shutil.which(cmd, path=path) is not None


def _git_get_branch():
    """Get current Git branch."""
    return _run(['git', 'rev-parse', '--abbrev-ref', 'HEAD']).stdout.decode()


def _git_checkout(branch):
    """Check out Git branch."""
    try:
        global ORIG_GIT_BRANCH
        if not ORIG_GIT_BRANCH:
            ORIG_GIT_BRANCH = _git_get_branch()

        logger.info('Checking out branch "%s"...', branch)
        # _run(['git', 'checkout', branch])
    except SubprocessError as e:
        raise Error('Failed to check out branch "%s". %s', branch, e.stderr.decode().capitalize())


def _cleanup():
    """Post-execution cleanup."""
    try:
        if ORIG_GIT_BRANCH:
            logger.info('Checking out original branch...')
            # _git_checkout(ORIG_GIT_BRANCH)
        return 0
    except Exception as e:
        logger.critical('Exception occurred during cleanup:', exc_info=e)
        return 1


def _split_version(version):
    if type(version) is not str or not re.match(r'^\d+\.\d+\.\d+$', version):
        raise Error('Invalid version number: %s', version)
    return version.split('.')


###########################################################################################
#                                      CLI Commands
###########################################################################################


class Command:
    """Command base class."""

    @classmethod
    def setup_arg_parser(cls, parser: argparse.ArgumentParser):
        pass

    def run(self, **kwargs):
        pass


class Check(Command):
    """Perform a dry-run check, nothing is changed."""

    @classmethod
    def setup_arg_parser(cls, parser: argparse.ArgumentParser):
        parser.add_argument('-v', '--version', help='Release version number or name')
        parser.add_argument('-s', '--src-dir', help='Source directory', default='.')
        parser.add_argument('-b', '--src-branch', help='Release source branch (default: inferred from --version)')
        parser.add_argument('-t', '--check-tools', help='Check for necessary build tools', action='store_true')

    def run(self, version, src_dir, src_branch, check_tools):
        if not version:
            logger.warning('No version specified, performing only basic checks.')

        if check_tools:
            logger.info('Checking for build tools...')
            self.check_transifex_cmd_exists()
            self.check_xcode_setup()

        logger.info('Performing basic checks...')
        self.check_src_dir_exists(src_dir)
        self.check_git_repository(src_dir)

        if version:
            logger.info('Performing version checks...')
            major, minor, patch = _split_version(version)
            src_branch = src_branch or f'release/{major}.{minor}.x'
            self.check_working_tree_clean(src_dir)
            self.check_release_does_not_exist(version, src_dir)
            self.check_source_branch_exists(src_branch, src_dir)
            _git_checkout(src_branch)
            logger.info('Attempting to find "%s" version string in source files...', version)
            self.check_version_in_cmake(version, src_dir)
            self.check_changelog(version, src_dir)
            self.check_app_stream_info(version, src_dir)

        logger.info('All checks passed.')

    @staticmethod
    def check_src_dir_exists(src_dir):
        if not src_dir:
            raise Error('Empty source directory given.')
        if not Path(src_dir).is_dir():
            raise Error(f'Source directory "{src_dir}" does not exist!')

    @staticmethod
    def check_output_dir_does_not_exist(output_dir):
        if Path(output_dir).is_dir():
            raise Error(f'Output directory "{output_dir}" already exists. Please choose a different folder.')

    @staticmethod
    def check_git_repository(cwd=None):
        if _run(['git', 'rev-parse', '--is-inside-work-tree'], check=False, cwd=cwd).returncode != 0:
            raise Error('Not a valid Git repository: %s', e.msg)

    @staticmethod
    def check_release_does_not_exist(tag_name, cwd=None):
        if _run(['git', 'tag', '-l', tag_name], check=False, cwd=cwd).stdout:
            raise Error('Release tag already exists: %s', tag_name)

    @staticmethod
    def check_working_tree_clean(cwd=None):
        if _run(['git', 'diff-index', '--quiet', 'HEAD', '--'], check=False, cwd=cwd).returncode != 0:
            raise Error('Current working tree is not clean! Please commit or unstage any changes.')

    @staticmethod
    def check_source_branch_exists(branch, cwd=None):
        if _run(['git', 'rev-parse', branch], check=False, cwd=cwd).returncode != 0:
            raise Error(f'Source branch "{branch}" does not exist!')

    @staticmethod
    def check_version_in_cmake(version, cwd=None):
        cmakelists = Path('CMakeLists.txt')
        if cwd:
            cmakelists = Path(cwd) / cmakelists
        if not cmakelists.is_file():
            raise Error('File not found: %s', cmakelists)
        cmakelists = cmakelists.read_text()
        major, minor, patch = _split_version(version)
        if f'{APP_NAME.upper()}_VERSION_MAJOR "{major}"' not in cmakelists:
            raise Error(f'{APP_NAME.upper()}_VERSION_MAJOR not updated to" {major}" in {cmakelists}.')
        if f'{APP_NAME.upper()}_VERSION_MINOR "{major}"' not in cmakelists:
            raise Error(f'{APP_NAME.upper()}_VERSION_MINOR not updated to "{minor}" in {cmakelists}.')
        if f'{APP_NAME.upper()}_VERSION_PATCH "{major}"' not in cmakelists:
            raise Error(f'{APP_NAME.upper()}_VERSION_PATCH not updated to "{patch}" in {cmakelists}.')

    @staticmethod
    def check_changelog(version, cwd=None):
        changelog = Path('CHANGELOG.md')
        if cwd:
            changelog = Path(cwd) / changelog
        if not changelog.is_file():
            raise Error('File not found: %s', changelog)
        major, minor, patch = _split_version(version)
        if not re.search(rf'^## {major}\.{minor}\.{patch} \([0-9]{4}-[0-9]{2}-[0-9]{2}\)',
                         changelog.read_text()):
            raise Error(f'{changelog} has not been updated to the "%s" release.', version)

    @staticmethod
    def check_app_stream_info(version, cwd=None):
        appstream = Path('share/linux/org.keepassxc.KeePassXC.appdata.xml')
        if cwd:
            appstream = Path(cwd) / appstream
        if not appstream.is_file():
            raise Error('File not found: %s', appstream)
        major, minor, patch = _split_version(version)
        if not re.search(rf'^\s*<release version="{major}\.{minor}\.{patch}" date="[0-9]{4}-[0-9]{2}-[0-9]{2}">',
                         appstream.read_text()):
            raise Error(f'{appstream} has not been updated to the "%s" release.', version)

    @staticmethod
    def check_transifex_cmd_exists():
        if not _cmd_exists('tx'):
            raise Error('Transifex tool "tx" is not installed! Please install it using "pip install transifex-client".')

    @staticmethod
    def check_xcode_setup():
        if sys.platform != 'darwin':
            return
        if not _cmd_exists('xcrun'):
            raise Error('xcrun command not found! Please check that you have correctly installed Xcode.')
        if not _cmd_exists('codesign'):
            raise Error('codesign command not found! Please check that you have correctly installed Xcode.')


class Merge(Command):
    """Merge release branch into main branch and create release tags."""

    @classmethod
    def setup_arg_parser(cls, parser: argparse.ArgumentParser):
        parser.add_argument('version', help='Release version number or name')

    def run(self, version):
        print(version)


class Build(Command):
    """Build and package binary release from sources."""

    @classmethod
    def setup_arg_parser(cls, parser: argparse.ArgumentParser):
        pass

    def run(self, **kwargs):
        pass


class GPGSign(Command):
    """Sign previously compiled release packages with GPG."""

    @classmethod
    def setup_arg_parser(cls, parser: argparse.ArgumentParser):
        pass

    def run(self, **kwargs):
        pass


class AppSign(Command):
    """Sign binaries with code signing certificates on Windows and macOS."""

    @classmethod
    def setup_arg_parser(cls, parser: argparse.ArgumentParser):
        pass

    def run(self, **kwargs):
        pass


class Notarize(Command):
    """Submit macOS application DMG for notarization."""

    @classmethod
    def setup_arg_parser(cls, parser: argparse.ArgumentParser):
        pass

    def run(self, **kwargs):
        pass


class I18N(Command):
    """Update translation files and pull from or push to Transifex."""

    @classmethod
    def setup_arg_parser(cls, parser: argparse.ArgumentParser):
        pass

    def run(self, **kwargs):
        pass


###########################################################################################
#                                       CLI Main
###########################################################################################


def main():
    if sys.platform == 'win32':
        # Enable terminal colours
        ctypes.windll.kernel32.SetConsoleMode(ctypes.windll.kernel32.GetStdHandle(-11), 7)

    sys.stderr.write(f'{LogFormatter.BOLD}{LogFormatter.GREEN}KeePassXC{LogFormatter.END}'
                     f'{LogFormatter.BOLD} Release Preparation Tool{LogFormatter.END}\n')
    sys.stderr.write(f'Copyright (C) 2016-{datetime.now().year} KeePassXC Team <https://keepassxc.org/>\n\n')

    parser = argparse.ArgumentParser(add_help=True)
    subparsers = parser.add_subparsers(title='commands')

    check_parser = subparsers.add_parser('check', help=Check.__doc__)
    Check.setup_arg_parser(check_parser)
    check_parser.set_defaults(_cmd=Check)

    merge_parser = subparsers.add_parser('merge', help=Merge.__doc__)
    Merge.setup_arg_parser(merge_parser)
    merge_parser.set_defaults(_cmd=Merge)

    build_parser = subparsers.add_parser('build', help=Build.__doc__)
    Build.setup_arg_parser(build_parser)
    build_parser.set_defaults(_cmd=Build)

    gpgsign_parser = subparsers.add_parser('gpgsign', help=GPGSign.__doc__)
    Merge.setup_arg_parser(gpgsign_parser)
    gpgsign_parser.set_defaults(_cmd=GPGSign)

    appsign_parser = subparsers.add_parser('appsign', help=AppSign.__doc__)
    AppSign.setup_arg_parser(appsign_parser)
    appsign_parser.set_defaults(_cmd=AppSign)

    notarize_parser = subparsers.add_parser('notarize', help=Notarize.__doc__)
    Notarize.setup_arg_parser(notarize_parser)
    notarize_parser.set_defaults(cm_cmdd=Notarize)

    i18n_parser = subparsers.add_parser('i18n', help=I18N.__doc__)
    I18N.setup_arg_parser(i18n_parser)
    i18n_parser.set_defaults(_cmd=I18N)

    args = parser.parse_args()
    if '_cmd' not in args:
        parser.print_help()
        return 1
    return args._cmd().run(**{k: v for k, v in vars(args).items() if k != '_cmd'}) or 0


def _sig_handler(_, __):
    logger.warning('Process interrupted.')
    sys.exit(3 | _cleanup())


signal.signal(signal.SIGINT, _sig_handler)
signal.signal(signal.SIGTERM, _sig_handler)

if __name__ == '__main__':
    ret = 0
    try:
        ret = main()
    except Error as e:
        logger.error(e.msg, *e.args, extra=e.kwargs)
        ret = e.kwargs.get('returncode', 1)
    except KeyboardInterrupt:
        logger.warning('Process interrupted.')
        ret = 3
    except Exception as e:
        logger.critical('Unhandled exception:', exc_info=e)
        ret = 4
    except SystemExit as e:
        ret = e.code
    finally:
        ret |= _cleanup()
        sys.exit(ret)
