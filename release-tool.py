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
from datetime import datetime
import logging
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys


###########################################################################################
#                                       Ctrl+F TOC
###########################################################################################

# class Check(Command)
# class Merge(Command)
# class Build(Command)
# class GPGSign(Command)
# class AppSign(Command)
# class Notarize(Command)
# class I18N(Command)


###########################################################################################
#                                         Globals
###########################################################################################


RELEASE_NAME = None
APP_NAME = 'KeePassXC'
SRC_DIR = os.getcwd()
GPG_KEY = 'BF5A669F2272CF4324C1FDA8CFB4C2166397D0D2'
GPG_GIT_KEY = None
OUTPUT_DIR = 'release'
ORIG_GIT_BRANCH_CWD = None
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
TRANSIFEX_RESOURCE = 'keepassxc.share-translations-keepassxc-en-ts--{}'
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


def _term_colors_on():
    return 'color' in os.getenv('TERM', '') or 'FORCE_COLOR' in os.environ or sys.platform == 'win32'


_TERM_BOLD = '\x1b[1m' if _term_colors_on() else ''
_TERM_RES_BOLD = '\x1b[22m' if _term_colors_on() else ''
_TERM_RED = '\x1b[31m' if _term_colors_on() else ''
_TERM_BRIGHT_RED = '\x1b[91m' if _term_colors_on() else ''
_TERM_YELLOW = '\x1b[33m' if _term_colors_on() else ''
_TERM_BLUE = '\x1b[34m' if _term_colors_on() else ''
_TERM_GREEN = '\x1b[32m' if _term_colors_on() else ''
_TERM_RES_CLR = '\x1b[39m' if _term_colors_on() else ''
_TERM_RES = '\x1b[0m' if _term_colors_on() else ''


class LogFormatter(logging.Formatter):
    _FMT = {
        logging.DEBUG: f'{_TERM_BOLD}[%(levelname)s]{_TERM_RES} %(message)s',
        logging.INFO: f'{_TERM_BOLD}[{_TERM_BLUE}%(levelname)s{_TERM_RES_CLR}]{_TERM_RES} %(message)s',
        logging.WARNING: f'{_TERM_BOLD}[{_TERM_YELLOW}%(levelname)s{_TERM_RES_CLR}]{_TERM_RES}{_TERM_YELLOW} %(message)s{_TERM_RES}',
        logging.ERROR: f'{_TERM_BOLD}[{_TERM_RED}%(levelname)s{_TERM_RES_CLR}]{_TERM_RES}{_TERM_RED} %(message)s{_TERM_RES}',
        logging.CRITICAL: f'{_TERM_BOLD}[{_TERM_BRIGHT_RED}%(levelname)s{_TERM_RES_CLR}]{_TERM_RES}{_TERM_BRIGHT_RED} %(message)s{_TERM_RES}',
    }

    def format(self, record):
        return logging.Formatter(self._FMT.get(record.levelno, '%(message)s')).format(record)


console_handler = logging.StreamHandler()
console_handler.setFormatter(LogFormatter())
logger = logging.getLogger(__file__)
logger.setLevel(os.getenv('LOGLEVEL') if 'LOGLEVEL' in os.environ else logging.INFO)
logger.addHandler(console_handler)


###########################################################################################
#                                      Helper Functions
###########################################################################################


def _get_bin_path(build_dir=None):
    if not build_dir:
        return os.getenv('PATH')
    build_dir = Path(build_dir)
    path_sep = ';' if sys.platform == 'win32' else ':'
    return path_sep.join(list(map(str, build_dir.rglob('vcpkg_installed/*/tools/**/bin'))) + [os.getenv('PATH')])


def _yes_no_prompt(prompt, default_no=True):
    sys.stderr.write(f'{prompt} {"[y/N]" if default_no else "[Y/n]"} ')
    yes_no = input().strip().lower()
    if default_no:
        return yes_no == 'y'
    return yes_no == 'n'


def _run(cmd, *args, cwd, path=None, env=None, input=None, capture_output=True, timeout=None, check=True, **kwargs):
    """
    Run a command and return its output.
    Raises an error if ``check`` is ``True`` and the process exited with a non-zero code.
    """
    if not cmd:
        raise ValueError('Empty command given.')

    if not env:
        env = os.environ.copy()
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
            raise SubprocessError('Command "%s" exited with non-zero code: %s',
                                  cmd[0], e.stderr.decode(), **e.__dict__)
        else:
            raise SubprocessError('Command "%s" exited with non-zero code.', cmd[0], **e.__dict__)


def _cmd_exists(cmd, path=None):
    """Check if command exists."""
    return shutil.which(cmd, path=path) is not None


def _git_working_dir_clean(*, cwd):
    """Check whether the Git working directory is clean."""
    return _run(['git', 'diff-index', '--quiet', 'HEAD', '--'], check=False, cwd=cwd).returncode == 0


def _git_get_branch(*, cwd):
    """Get current Git branch."""
    return _run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=cwd).stdout.decode().strip()


def _git_branches_related(branch1, branch2, *, cwd):
    """Check whether branch is ancestor or descendant of another."""
    return (_run(['git', 'merge-base', '--is-ancestor', branch1, branch2], cwd=cwd).returncode == 0 or
            _run(['git', 'merge-base', '--is-ancestor', branch2, branch1], cwd=cwd).returncode == 0)


def _git_checkout(branch, *, cwd):
    """Check out Git branch."""
    try:
        global ORIG_GIT_BRANCH_CWD
        if not ORIG_GIT_BRANCH_CWD:
            ORIG_GIT_BRANCH_CWD = (_git_get_branch(cwd=cwd), cwd)

        logger.info('Checking out branch "%s"...', branch)
        # _run(['git', 'checkout', branch], cwd=cwd)
    except SubprocessError as e:
        raise Error('Failed to check out branch "%s". %s', branch, e.stderr.decode())


def _git_commit_files(files, message, *, cwd, sign_key=None):
    """Commit changes to files or directories."""
    _run(['git', 'reset'], cwd=cwd)
    _run(['git', 'add', *files], cwd=cwd)

    if _git_working_dir_clean(cwd=cwd):
        logger.info('No changes to commit.')
        return

    logger.info('Committing changes...')
    commit_args = ['git', 'commit', '--message', message]
    if sign_key:
        commit_args.extend(['--gpg-sign', sign_key])
    _run(commit_args, cwd=cwd, capture_output=False)


def _cleanup():
    """Post-execution cleanup."""
    try:
        if ORIG_GIT_BRANCH_CWD:
            logger.info('Checking out original branch...')
            # _git_checkout(ORIG_GIT_BRANCH_CWD[0], cwd=ORIG_GIT_BRANCH_CWD[1])
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

    def __init__(self, arg_parser):
        self._arg_parser = arg_parser

    @classmethod
    def setup_arg_parser(cls, parser: argparse.ArgumentParser):
        pass

    def run(self, **kwargs):
        pass


class Check(Command):
    """Perform a dry-run check, nothing is changed."""

    @classmethod
    def setup_arg_parser(cls, parser: argparse.ArgumentParser):
        parser.add_argument('-v', '--version', help='Release version number or name.')
        parser.add_argument('-s', '--src-dir', help='Source directory.', default='.')
        parser.add_argument('-b', '--release-branch', help='Release source branch (default: inferred from --version).')
        parser.add_argument('-t', '--check-tools', help='Check for necessary build tools.', action='store_true')

    def run(self, version, src_dir, release_branch, check_tools):
        if not version:
            logger.warning('No version specified, performing only basic checks.')
        if check_tools:
            self.perform_tool_checks()
        self.perform_basic_checks(src_dir)
        if version:
            self.perform_version_checks(version, src_dir, release_branch)
        logger.info('All checks passed.')

    @classmethod
    def perform_tool_checks(cls):
        logger.info('Checking for required build tools...')
        cls.check_xcode_setup()

    @classmethod
    def perform_basic_checks(cls, src_dir):
        logger.info('Performing basic checks...')
        cls.check_src_dir_exists(src_dir)
        cls.check_git_repository(src_dir)

    @classmethod
    def perform_version_checks(cls, version, src_dir, release_branch=None):
        logger.info('Performing version checks...')
        major, minor, patch = _split_version(version)
        src_branch = release_branch or f'release/{major}.{minor}.x'
        cls.check_working_tree_clean(src_dir)
        cls.check_release_does_not_exist(version, src_dir)
        cls.check_branch_exists(src_branch, src_dir)
        _git_checkout(src_branch, cwd=src_dir)
        logger.info('Attempting to find "%s" version string in source files...', version)
        cls.check_version_in_cmake(version, src_dir)
        cls.check_changelog(version, src_dir)
        cls.check_app_stream_info(version, src_dir)

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
    def check_git_repository(cwd):
        if _run(['git', 'rev-parse', '--is-inside-work-tree'], check=False, cwd=cwd).returncode != 0:
            raise Error('Not a valid Git repository: %s', e.msg)

    @staticmethod
    def check_release_does_not_exist(tag_name, cwd):
        if _run(['git', 'tag', '-l', tag_name], check=False, cwd=cwd).stdout:
            raise Error('Release tag already exists: %s', tag_name)

    @staticmethod
    def check_working_tree_clean(cwd):
        if not _git_working_dir_clean(cwd=cwd):
            raise Error('Current working tree is not clean! Please commit or unstage any changes.')

    @staticmethod
    def check_branch_exists(branch, cwd):
        if _run(['git', 'rev-parse', branch], check=False, cwd=cwd).returncode != 0:
            raise Error(f'Branch "{branch}" does not exist!')

    @staticmethod
    def check_version_in_cmake(version, cwd):
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
    def check_changelog(version, cwd):
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
    def check_app_stream_info(version, cwd):
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
        parser.add_argument('version', help='Release version number or name.')
        parser.add_argument('-s', '--src-dir', help='Source directory.', default='.')
        parser.add_argument('-b', '--release-branch', help='Release source branch (default: inferred from --version).')
        parser.add_argument('-k', '--sign-key', help='PGP key for signing merge commits.')
        parser.add_argument('-t', '--tag-name', help='Name of tag to create (default: infer from version).')

    def run(self, version, src_dir, release_branch, sign_key, tag_name):
        pass


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
        parser.add_argument('-s', '--src-dir', help='Source directory.', default='.')
        parser.add_argument('-b', '--branch', help='Branch to operate on.')
        parser.add_argument('-c', '--commit', help='Commit changes.', action='store_true')

        subparsers = parser.add_subparsers(title='Subcommands', dest='subcmd')
        push = subparsers.add_parser('tx-push', help='Push source translation file to Transifex.')
        push.add_argument('-r', '--resource', help='Transifex resource name.', choices=['master', 'develop'])
        push.add_argument('-y', '--yes', help='Don\'t ask before pushing source file.', action='store_true')
        push.add_argument('tx_args', help='Additional arguments to pass to tx subcommand.', nargs=argparse.REMAINDER)

        pull = subparsers.add_parser('tx-pull', help='Pull updated translations from Transifex.')
        pull.add_argument('-r', '--resource', help='Transifex resource name.', choices=['master', 'develop'])
        pull.add_argument('-m', '--min-perc', help='Minimum percent complete for pull (default: %(default)s).',
                          choices=range(0, 101), metavar='[0-100]', default=60)
        pull.add_argument('-y', '--yes', help='Don\'t ask before pulling translations.', action='store_true')
        pull.add_argument('tx_args', help='Additional arguments to pass to tx subcommand.', nargs=argparse.REMAINDER)

        lupdate = subparsers.add_parser('lupdate', help='Update source translation file from C++ sources.')
        lupdate.add_argument('-d', '--build-dir', help='Build directory for looking up lupdate binary.')
        lupdate.add_argument('lupdate_args', help='Additional arguments to pass to lupdate subcommand.',
                             nargs=argparse.REMAINDER)

    @staticmethod
    def check_transifex_cmd_exists():
        if not _cmd_exists('tx'):
            raise Error(f'Transifex tool "tx" is not installed! Installation instructions: '
                        f'{_TERM_BOLD}https://developers.transifex.com/docs/cli{_TERM_RES}.')

    @staticmethod
    def check_transifex_config_exists(src_dir):
        if not (Path(src_dir) / '.tx' / 'config').is_file():
            raise Error('No Transifex config found in source dir.')
        if not (Path.home() / '.transifexrc').is_file():
            raise Error('Transifex API key not configured. Run "tx status" first.')

    @staticmethod
    def check_lupdate_exists(path):
        if _cmd_exists('lupdate', path=path):
            result = _run(['lupdate', '-version'], path=path, check=False, cwd=None)
            if result.returncode == 0 and result.stdout.decode().startswith('lupdate version 5.'):
                return
        raise Error('lupdate command not found. Make sure it is installed and the correct version.')

    def run(self, subcmd, src_dir, branch, commit, **kwargs):
        if not subcmd:
            logger.error('No subcommand specified.')
            self._arg_parser.parse_args(['i18n', '--help'])

        Check.perform_basic_checks(src_dir)
        if branch:
            Check.check_working_tree_clean(src_dir)
            Check.check_branch_exists(branch, src_dir)
            _git_checkout(branch, cwd=src_dir)

        if subcmd.startswith('tx-'):
            self.check_transifex_cmd_exists()
            self.check_transifex_config_exists(src_dir)

            if not kwargs['resource'] and _git_branches_related('develop', 'HEAD', cwd=src_dir):
                logger.info(f'Branch derives from develop, using {_TERM_BOLD}"develop"{_TERM_RES_BOLD} resource.')
                kwargs['resource'] = 'develop'
            elif not kwargs['resource']:
                logger.info(f'Release branch, using {_TERM_BOLD}"master"{_TERM_RES_BOLD} resource.')
                kwargs['resource'] = 'master'

            kwargs['resource'] = TRANSIFEX_RESOURCE.format(kwargs['resource'])
            kwargs['tx_args'] = kwargs['tx_args'][1:]
            if subcmd == 'tx-push':
                self.run_tx_push(src_dir, **kwargs)
            elif subcmd == 'tx-pull':
                self.run_tx_pull(src_dir, **kwargs)

        elif subcmd == 'lupdate':
            kwargs['lupdate_args'] = kwargs['lupdate_args'][1:]
            self.run_lupdate(src_dir, **kwargs)

    @staticmethod
    def run_tx_push(src_dir, resource, yes, tx_args):
        sys.stderr.write(f'\nAbout to push the {_TERM_BOLD}"en"{_TERM_RES} source file from the '
                         f'current branch to Transifex:\n')
        sys.stderr.write(f'    {_TERM_BOLD}{_git_get_branch(cwd=src_dir)}{_TERM_RES}'
                         f' -> {_TERM_BOLD}{resource}{_TERM_RES}\n')
        if not yes and not _yes_no_prompt('Continue?'):
            logger.error('Push aborted.')
            return
        logger.info('Pushing source file to Transifex...')
        _run(['tx', 'push', '--source', '--use-git-timestamps', *tx_args, resource],
             cwd=src_dir, capture_output=False)
        logger.info('Push successful.')

    @staticmethod
    def run_tx_pull(src_dir, resource, min_perc, yes, tx_args):
        sys.stderr.write(f'\nAbout to pull translations for {_TERM_BOLD}"{resource}"{_TERM_RES_BOLD}.\n')
        if not yes and not _yes_no_prompt('Continue?'):
            logger.error('Pull aborted.')
            return
        logger.info('Pulling translations from Transifex...')
        _run(['tx', 'pull', '--all', '--use-git-timestamps', f'--minimum-perc={min_perc}', *tx_args, resource],
             cwd=src_dir, capture_output=False)
        logger.info('Pull successful.')
        files = [f.relative_to(src_dir) for f in Path(src_dir).glob('share/translations/*.ts')]
        _git_commit_files(files, 'Update translations.', cwd=src_dir)

    def run_lupdate(self, src_dir, build_dir=None, lupdate_args=None):
        path = _get_bin_path(build_dir)
        self.check_lupdate_exists(path)
        logger.info('Updating translation source files from C++ sources...')
        _run(['lupdate', '-no-ui-lines', '-disable-heuristic', 'similartext', '-locations', 'none',
              '-extensions', 'c,cpp,h,js,mm,qrc,ui', '-no-obsolete', 'src',
              '-ts', str(Path(f'share/translations/{APP_NAME.lower()}_en.ts')), *(lupdate_args or [])],
             cwd=src_dir, path=path, capture_output=False)
        logger.info('Translation source files updated.')
        _git_commit_files([f'share/translations/{APP_NAME.lower()}_en.ts'], 'Update translation sources.', cwd=src_dir)


###########################################################################################
#                                       CLI Main
###########################################################################################


def main():
    if sys.platform == 'win32':
        # Enable terminal colours
        ctypes.windll.kernel32.SetConsoleMode(ctypes.windll.kernel32.GetStdHandle(-11), 7)

    sys.stderr.write(f'{_TERM_BOLD}{_TERM_GREEN}KeePassXC{_TERM_RES}'
                     f'{_TERM_BOLD} Release Preparation Tool{_TERM_RES}\n')
    sys.stderr.write(f'Copyright (C) 2016-{datetime.now().year} KeePassXC Team <https://keepassxc.org/>\n\n')

    parser = argparse.ArgumentParser(add_help=True)
    subparsers = parser.add_subparsers(title='Commands')

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
    notarize_parser.set_defaults(_cmd=Notarize)

    i18n_parser = subparsers.add_parser('i18n', help=I18N.__doc__)
    I18N.setup_arg_parser(i18n_parser)
    i18n_parser.set_defaults(_cmd=I18N)

    args = parser.parse_args()
    if '_cmd' not in args:
        parser.print_help()
        return 1
    return args._cmd(parser).run(**{k: v for k, v in vars(args).items() if k != '_cmd'}) or 0


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
