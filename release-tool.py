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
import platform
import random
import re
import signal
import shutil
import stat
import string
import subprocess
import sys
import tempfile
from urllib.request import urlretrieve


###########################################################################################
#                                       Ctrl+F TOC
###########################################################################################

# class Check(Command)
# class Merge(Command)
# class Build(Command)
# class AppSign(Command)
# class GPGSign(Command)
# class I18N(Command)


###########################################################################################
#                                         Globals
###########################################################################################


RELEASE_NAME = None
SRC_DIR = os.getcwd()
GIT_SIGN_KEY = 'BF5A669F2272CF4324C1FDA8CFB4C2166397D0D2'
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
TRANSIFEX_PULL_PERC = 60
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

    def __str__(self):
        return self.msg % self.args


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


def _choice_prompt(prompt, choices):
    while True:
        sys.stderr.write(prompt + '\n')
        for i, c in enumerate(choices):
            sys.stderr.write(f'  {i + 1}) {c}\n')
        sys.stderr.write('\nYour choice: ')
        choice = input().strip()
        if not choice.isnumeric() or int(choice) < 1 or int(choice) > len(choices):
            logger.error('Invalid choice: %s', choice)
            continue
        return int(choice) - 1


def _run(cmd, *args, cwd, path=None, env=None, input=None, capture_output=True, timeout=None, check=True,
         docker_image=None, docker_privileged=False, docker_mounts=None, docker_platform=None, **run_kwargs):
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
    if _term_colors_on():
        env['FORCE_COLOR'] = '1'

    if docker_image:
        docker_cmd = ['docker', 'run', '--rm', '--tty=true', f'--workdir={cwd}', f'--user={os.getuid()}:{os.getgid()}']
        docker_cmd.extend([f'--env={k}={v}' for k, v in env.items() if k in ['FORCE_COLOR', 'CC', 'CXX']])
        if path:
            docker_cmd.append(f'--env=PATH={path}')
        docker_cmd.append(f'--volume={Path(cwd).absolute()}:{Path(cwd).absolute()}:rw')
        if docker_mounts:
            docker_cmd.extend([f'--volume={Path(d).absolute()}:{Path(d).absolute()}:rw' for d in docker_mounts])
        if docker_privileged:
            docker_cmd.extend(['--cap-add=SYS_ADMIN', '--security-opt=apparmor:unconfined', '--device=/dev/fuse'])
        if docker_platform:
            docker_cmd.append(f'--platform={docker_platform}')
        docker_cmd.append(docker_image)
        cmd = docker_cmd + cmd

    try:
        logger.debug('Running command: %s', ' '.join(cmd))
        return subprocess.run(
            cmd, *args,
            input=input,
            capture_output=capture_output,
            cwd=cwd,
            env=env,
            timeout=timeout,
            check=check,
            **run_kwargs)
    except FileNotFoundError:
        raise Error('Command not found: %s', cmd[0] if type(cmd) in [list, tuple] else cmd)
    except subprocess.CalledProcessError as e:
        if e.stderr:
            err_txt = e.stderr
            if type(err_txt) is bytes:
                err_txt = err_txt.decode()
            raise SubprocessError('Command "%s" exited with non-zero code: %s',
                                  cmd[0], err_txt, **e.__dict__)
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
    return _run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=cwd, text=True).stdout.strip()


def _git_branches_related(branch1, branch2, *, cwd):
    """Check whether branch is ancestor or descendant of another."""
    return (_run(['git', 'merge-base', '--is-ancestor', branch1, branch2], cwd=cwd).returncode == 0 or
            _run(['git', 'merge-base', '--is-ancestor', branch2, branch1], cwd=cwd).returncode == 0)


def _git_checkout(branch, *, cwd):
    """Check out Git branch."""
    try:
        global ORIG_GIT_BRANCH_CWD
        if not ORIG_GIT_BRANCH_CWD:
            ORIG_GIT_BRANCH_CWD = (_git_get_branch(cwd=cwd, text=True), cwd)

        logger.info('Checking out branch "%s"...', branch)
        # _run(['git', 'checkout', branch], cwd=cwd, text=True)
    except SubprocessError as e:
        raise Error('Failed to check out branch "%s". %s', branch, e)


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
    """Perform a pre-merge dry-run check, nothing is changed."""

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
    def perform_version_checks(cls, version, src_dir, git_ref=None, version_exists=False, checkout=True):
        logger.info('Performing version checks...')
        major, minor, patch = _split_version(version)
        cls.check_working_tree_clean(src_dir)
        if version_exists:
            git_ref = git_ref or version
            cls.check_release_exists(git_ref, src_dir)
        else:
            git_ref = git_ref or f'release/{major}.{minor}.x'
            cls.check_release_does_not_exist(version, src_dir)
            cls.check_branch_exists(git_ref, src_dir)
        if checkout:
            _git_checkout(git_ref, cwd=src_dir)
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
    def check_git_repository(cwd):
        if _run(['git', 'rev-parse', '--is-inside-work-tree'], check=False, cwd=cwd).returncode != 0:
            raise Error('Not a valid Git repository: %s', e.msg)

    @staticmethod
    def check_release_exists(tag_name, cwd):
        if not _run(['git', 'tag', '--list', tag_name], check=False, cwd=cwd).stdout:
            raise Error('Release tag does not exists: %s', tag_name)

    @staticmethod
    def check_release_does_not_exist(tag_name, cwd):
        if _run(['git', 'tag', '--list', tag_name], check=False, cwd=cwd).stdout:
            raise Error('Release tag already exists: %s', tag_name)

    @staticmethod
    def check_working_tree_clean(cwd):
        # TODO: Remove
        return
        if not _git_working_dir_clean(cwd=cwd):
            raise Error('Current working tree is not clean! Please commit or unstage any changes.')

    @staticmethod
    def check_branch_exists(branch, cwd):
        if _run(['git', 'rev-parse', branch], check=False, cwd=cwd).returncode != 0:
            raise Error(f'Branch or tag "{branch}" does not exist!')

    @staticmethod
    def check_version_in_cmake(version, cwd):
        cmakelists = Path('CMakeLists.txt')
        if cwd:
            cmakelists = Path(cwd) / cmakelists
        if not cmakelists.is_file():
            raise Error('File not found: %s', cmakelists)
        cmakelists_text = cmakelists.read_text()
        major = re.search(r'^set\(KEEPASSXC_VERSION_MAJOR "(\d+)"\)$', cmakelists_text, re.MULTILINE).group(1)
        minor = re.search(r'^set\(KEEPASSXC_VERSION_MINOR "(\d+)"\)$', cmakelists_text, re.MULTILINE).group(1)
        patch = re.search(r'^set\(KEEPASSXC_VERSION_PATCH "(\d+)"\)$', cmakelists_text, re.MULTILINE).group(1)
        cmake_version = '.'.join([major, minor, patch])
        if cmake_version != version:
            raise Error(f'Version number in {cmakelists} not updated! Expected: %s, found: %s.', version, cmake_version)

    @staticmethod
    def check_changelog(version, cwd):
        changelog = Path('CHANGELOG.md')
        if cwd:
            changelog = Path(cwd) / changelog
        if not changelog.is_file():
            raise Error('File not found: %s', changelog)
        major, minor, patch = _split_version(version)
        if not re.search(rf'^## {major}\.{minor}\.{patch} \(.+?\)\n+', changelog.read_text(), re.MULTILINE):
            raise Error(f'{changelog} has not been updated to the "%s" release.', version)

    @staticmethod
    def check_app_stream_info(version, cwd):
        appstream = Path('share/linux/org.keepassxc.KeePassXC.appdata.xml')
        if cwd:
            appstream = Path(cwd) / appstream
        if not appstream.is_file():
            raise Error('File not found: %s', appstream)
        major, minor, patch = _split_version(version)
        if not re.search(rf'^\s*<release version="{major}\.{minor}\.{patch}" date=".+?">',
                         appstream.read_text(), re.MULTILINE):
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
        parser.add_argument('-b', '--release-branch', help='Release source branch (default: inferred from version).')
        parser.add_argument('-t', '--tag-name', help='Name of tag to create (default: same as version).')
        parser.add_argument('-l', '--no-latest', help='Don\'t advance "latest" tag.', action='store_true')
        parser.add_argument('-k', '--sign-key', default=GIT_SIGN_KEY,
                            help='PGP key for signing merge commits (default: %(default)s).')
        parser.add_argument('--no-sign', help='Don\'t sign release tags (for testing only!)', action='store_true')
        parser.add_argument('-y', '--yes', help='Bypass confirmation prompts.', action='store_true')
        parser.add_argument('--skip-translations', help='Skip pulling translations from Transifex', action='store_true')
        parser.add_argument('--tx-resource', help='Transifex resource name.', choices=['master', 'develop'])
        parser.add_argument('--tx-min-perc', choices=range(0, 101), metavar='[0-100]', default=TRANSIFEX_PULL_PERC,
                            help='Minimum percent complete for Transifex pull (default: %(default)s).')

    def run(self, version, src_dir, release_branch, tag_name, no_latest, sign_key, no_sign, yes,
            skip_translations, tx_resource, tx_min_perc):
        major, minor, patch = _split_version(version)
        Check.perform_basic_checks(src_dir)
        Check.perform_version_checks(version, src_dir, release_branch)

        # Update translations
        if not skip_translations:
            i18n = I18N(self._arg_parser)
            i18n.run_tx_pull(src_dir, i18n.derive_resource_name(tx_resource, cwd=src_dir), tx_min_perc,
                             commit=True, yes=yes)

        changelog = re.search(rf'^## ({major}\.{minor}\.{patch} \(.*?\)\n\n+.+?)\n\n+## ',
                              (Path(src_dir) / 'CHANGELOG.md').read_text(), re.MULTILINE | re.DOTALL)
        if not changelog:
            raise Error(f'No changelog entry found for version {version}.')
        changelog = 'Release ' + changelog.group(1)

        tag_name = tag_name or version
        logger.info('Creating "%s%" tag...', tag_name)
        tag_cmd = ['git', 'tag', '--annotate', tag_name, '--message', changelog]
        if not no_sign:
            tag_cmd.extend(['--sign', '--local-user', sign_key])
        _run(tag_cmd, cwd=src_dir)

        if not no_latest:
            logger.info('Advancing "latest" tag...')
            tag_cmd = ['git', 'tag', '--annotate', 'latest', '--message', 'Latest stable release', '--force']
            if not no_sign:
                tag_cmd.extend(['--sign', '--local-user', sign_key])
            _run(tag_cmd, cwd=src_dir)

        log_msg = ('All done! Don\'t forget to push the release branch and the new tags:\n'
                   f'    {_TERM_BOLD}git push origin {release_branch}{_TERM_RES}\n'
                   f'    {_TERM_BOLD}git push origin tag {tag_name}{_TERM_RES}')
        if not no_latest:
            log_msg += f'\n    {_TERM_BOLD}git push origin tag latest --force{_TERM_RES}'
        logger.info(log_msg)


class Build(Command):
    """Build and package binary release from sources."""

    @classmethod
    def setup_arg_parser(cls, parser: argparse.ArgumentParser):
        parser.add_argument('version', help='Release version number or name.')
        parser.add_argument('-s', '--src-dir', help='Source directory.', default='.')
        parser.add_argument('-t', '--tag-name', help='Name of the tag to check out (default: same as version).')
        parser.add_argument('-o', '--output-dir', default='release',
                            help='Build output directory (default: %(default)s.')
        parser.add_argument('-g', '--cmake-generator', help='Override default CMake generator.')
        parser.add_argument('-i', '--install-prefix', default='/usr/local',
                            help='Build install prefix (default: %(default)s).')
        parser.add_argument('-n', '--no-source-tarball', help='Don\'t create a source tarball.', action='store_true')
        parser.add_argument('--snapshot', help='Build snapshot from current HEAD.', action='store_true')
        parser.add_argument('--use-system-deps', help='Use system dependencies instead of vcpkg.', action='store_true')
        parser.add_argument('-j', '--parallelism', default=os.cpu_count(), type=int,
                            help='Build parallelism (default: %(default)s).')
        parser.add_argument('-y', '--yes', help='Bypass confirmation prompts.', action='store_true')

        if sys.platform == 'darwin':
            parser.add_argument('--macos-target', default=MACOSX_DEPLOYMENT_TARGET, metavar='MACOSX_DEPLOYMENT_TARGET',
                                help='macOS deployment target version (default: %(default)s).')
            parser.add_argument('-p', '--platform-target', default=platform.uname().machine,
                                help='Build target platform (default: %(default)s).', choices=['x86_64', 'arm64'])
        elif sys.platform == 'linux':
            parser.add_argument('-d', '--docker-image', help='Run build in Docker image (overrides --use-system-deps).')
            parser.add_argument('-p', '--platform-target', help='Build target platform (default: %(default)s).',
                                choices=['x86_64', 'aarch64'], default=platform.uname().machine)
            parser.add_argument('-a', '--appimage', help='Build an AppImage.', action='store_true')

        parser.add_argument('-c', '--cmake-opts', nargs=argparse.REMAINDER,
                            help='Additional CMake options (no other arguments can be specified after this).')

    def run(self, version, src_dir, output_dir, tag_name, snapshot, no_source_tarball, cmake_generator, yes, **kwargs):
        Check.perform_basic_checks(src_dir)
        src_dir = Path(src_dir).resolve()
        output_dir = Path(output_dir)
        if output_dir.is_dir():
            logger.warning(f'Output directory "{output_dir}" already exists.')
            if not yes and not _yes_no_prompt('Reuse existing output directory?'):
                raise Error('Build aborted!')
        else:
            logger.debug('Creating output directory...')
            output_dir.mkdir(parents=True)

        tag_name = tag_name or version
        cmake_opts = [
            '-DWITH_XC_ALL=ON',
            '-DCMAKE_BUILD_TYPE=Release',
            '-DCMAKE_INSTALL_PREFIX=' + kwargs['install_prefix'],
            '-DWITH_TESTS=OFF',
            '-DWITH_GUI_TESTS=OFF',
        ]
        if not kwargs['use_system_deps'] and not kwargs.get('docker_image'):
            cmake_opts.append(f'-DCMAKE_TOOLCHAIN_FILE={self._get_vcpkg_toolchain_file()}')

        if snapshot:
            logger.info('Building a snapshot from HEAD.')
            try:
                Check.check_version_in_cmake(version, src_dir)
            except Error as e:
                logger.warning(e.msg, *e.args)
                cmake_opts.append(f'-DOVERRIDE_VERSION={version}-snapshot')
            cmake_opts.append('-DKEEPASSXC_BUILD_TYPE=Snapshot')
            version += '-snapshot'
            tag_name = 'HEAD'
        else:
            Check.perform_version_checks(version, src_dir, tag_name, version_exists=True, checkout=True)
            cmake_opts.append('-DKEEPASSXC_BUILD_TYPE=Release')

        if cmake_generator:
            cmake_opts.extend(['-G', cmake_generator])
        kwargs['cmake_opts'] = cmake_opts + (kwargs['cmake_opts'] or [])

        if not no_source_tarball:
            self.build_source_tarball(version, tag_name, src_dir, output_dir)

        if sys.platform == 'win32':
            return self.build_windows(version, src_dir, output_dir, **kwargs)
        if sys.platform == 'darwin':
            return self.build_macos(version, src_dir, output_dir, **kwargs)
        if sys.platform == 'linux':
            return self.build_linux(version, src_dir, output_dir, **kwargs)
        raise Error('Unsupported build platform: %s', sys.platform)

    @staticmethod
    def _get_vcpkg_toolchain_file(path=None):
        vcpkg = shutil.which('vcpkg', path=path)
        if not vcpkg:
            raise Error('vcpkg not found in PATH (use --use-system-deps to build with system dependencies instead).')
        toolchain = Path(vcpkg).parent / 'scripts' / 'buildsystems' / 'vcpkg.cmake'
        if not toolchain.is_file():
            raise Error('Toolchain file not found in vcpkg installation directory.')
        return toolchain.resolve()

    # noinspection PyMethodMayBeStatic
    def build_source_tarball(self, version, tag_name, src_dir, output_dir):
        if not shutil.which('tar'):
            logger.warning('tar not installed, skipping source tarball creation.')
            return

        logger.info('Building source tarball...')
        prefix = f'keepassxc-{version}'
        output_file = Path(output_dir) / f'{prefix}-src.tar'
        _run(['git', 'archive', '--format=tar', f'--prefix={prefix}/', f'--output={output_file.absolute()}', tag_name],
             cwd=src_dir)

        # Add .version and .gitrev files to tarball
        with tempfile.TemporaryDirectory() as tmp:
            tpref = Path(tmp) / prefix
            tpref.mkdir()
            fver = tpref / '.version'
            fver.write_text(version)
            frev = tpref / '.gitrev'
            git_rev = _run(['git', 'rev-parse', '--short=7', tag_name], cwd=src_dir, text=True).stdout.strip()
            frev.write_text(git_rev)
            _run(['tar', '--append', f'--file={output_file.absolute()}',
                  str(frev.relative_to(tmp)), str(fver.relative_to(tmp))], cwd=tmp)

        logger.debug('Compressing source tarball...')
        comp = shutil.which('xz')
        if not comp:
            logger.warning('xz not installed, falling back to bzip2.')
            comp = 'bzip2'
        _run([comp, '-6', '--force', str(output_file.absolute())], cwd=src_dir)

    # noinspection PyMethodMayBeStatic
    def build_windows(self, version, src_dir, output_dir, *, parallelism, cmake_opts, **_):
        pass

    # noinspection PyMethodMayBeStatic
    def build_macos(self, version, src_dir, output_dir, *,  use_system_deps, parallelism, cmake_opts,
                    macos_target, platform_target, **_):
        if not use_system_deps:
            cmake_opts.append(f'-DVCPKG_TARGET_TRIPLET={platform_target.replace("86_", "")}-osx-dynamic-release')
        cmake_opts.append(f'-DCMAKE_OSX_DEPLOYMENT_TARGET={macos_target}')
        cmake_opts.append(f'-DCMAKE_OSX_ARCHITECTURES={platform_target}')

        with tempfile.TemporaryDirectory() as build_dir:
            logger.info('Configuring build...')
            _run(['cmake', *cmake_opts, str(src_dir)], cwd=build_dir, capture_output=False)

            logger.info('Compiling sources...')
            _run(['cmake', '--build', '.', f'--parallel', str(parallelism)], cwd=build_dir, capture_output=False)

            logger.info('Packaging application...')
            _run(['cpack', '-G', 'DragNDrop'], cwd=build_dir, capture_output=False)

            output_file = Path(build_dir) / f'KeePassXC-{version}.dmg'
            output_file.rename(output_dir / f'KeePassXC-{version}-{platform_target}-unsigned.dmg')

        logger.info('All done! Please don\'t forget to sign the binaries before distribution.')

    @staticmethod
    def _download_tools_if_not_available(toolname, bin_dir, url, docker_args=None):
        if _run(['which', toolname], cwd=None, check=False, **(docker_args or {})).returncode != 0:
            logger.info(f'Downloading {toolname}...')
            outfile = bin_dir / toolname
            urlretrieve(url, outfile)
            outfile.chmod(outfile.stat().st_mode | stat.S_IEXEC)

    def build_linux(self, version, src_dir, output_dir, *, install_prefix, parallelism, cmake_opts, use_system_deps,
                    platform_target, appimage, docker_image, **_):
        if use_system_deps and platform_target != platform.uname().machine and not docker_image:
            raise Error('Need --docker-image for cross-platform compilation when not building with vcpkg!')

        docker_args = dict(
            docker_image=docker_image,
            docker_mounts=[src_dir],
            docker_platform=f'linux/{platform_target}',
        )
        if docker_image:
            logger.info('Pulling Docker image...')
            _run(['docker', 'pull', f'--platform=linux/{platform_target}', docker_image],
                 cwd=None, capture_output=False)

        if appimage:
            cmake_opts.append('-DKEEPASSXC_DIST_TYPE=AppImage')

        with tempfile.TemporaryDirectory() as build_dir:
            logger.info('Configuring build...')
            _run(['cmake', *cmake_opts, str(src_dir)], cwd=build_dir, capture_output=False, **docker_args)

            logger.info('Compiling sources...')
            _run(['cmake', '--build', '.', '--parallel', str(parallelism)],
                 cwd=build_dir, capture_output=False, **docker_args)

            logger.info('Bundling AppDir...')
            app_dir = Path(build_dir) / f'KeePassXC-{version}-{platform_target}.AppDir'
            _run(['cmake', '--install', '.', '--strip',
                  '--prefix', (app_dir.absolute() / install_prefix.lstrip('/')).as_posix()],
                 cwd=build_dir, capture_output=False, **docker_args)
            shutil.copytree(app_dir, output_dir / app_dir.name, symlinks=True)

            if appimage:
                self._build_linux_appimage(
                    version, src_dir, output_dir, app_dir, build_dir, install_prefix, platform_target, docker_args)

    def _build_linux_appimage(self, version, src_dir, output_dir, app_dir, build_dir, install_prefix,
                              platform_target, docker_args):
        if (app_dir / 'AppRun').exists():
            raise Error('AppDir has already been run through linuxdeploy! Please create a fresh AppDir and try again.')

        bin_dir = Path(build_dir) / 'bin'
        bin_dir.mkdir()
        self._download_tools_if_not_available(
            'linuxdeploy', bin_dir,
            'https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/' +
            f'linuxdeploy-{platform_target}.AppImage',
            docker_args)
        self._download_tools_if_not_available(
            'linuxdeploy-plugin-qt', bin_dir,
            'https://github.com/linuxdeploy/linuxdeploy-plugin-qt/releases/download/continuous/' +
            f'linuxdeploy-plugin-qt-{platform_target}.AppImage',
            docker_args)
        self._download_tools_if_not_available(
            'appimagetool', bin_dir,
            'https://github.com/AppImage/AppImageKit/releases/download/continuous/' +
            f'appimagetool-{platform_target}.AppImage',
            docker_args)

        env_path = ':'.join([bin_dir.as_posix(), _get_bin_path()])
        install_prefix = app_dir / install_prefix.lstrip('/')
        desktop_file = install_prefix / 'share/applications/org.keepassxc.KeePassXC.desktop'
        icon_file = install_prefix / 'share/icons/hicolor/256x256/apps/keepassxc.png'
        executables = (install_prefix / 'bin').glob('keepassxc*')
        app_run = src_dir / 'share/linux/appimage-apprun.sh'

        logger.info('Building AppImage...')
        logger.debug('Running linuxdeploy...')
        _run(['linuxdeploy', '--plugin=qt', f'--appdir={app_dir}', f'--custom-apprun={app_run}',
              f'--desktop-file={desktop_file}', f'--icon-file={icon_file}',
              *[f'--executable={ex}' for ex in executables]],
             cwd=build_dir, capture_output=False, path=env_path, **docker_args)

        logger.debug('Running appimagetool...')
        appimage_name = f'KeePassXC-{version}-{platform_target}.AppImage'
        desktop_file.write_text(desktop_file.read_text().strip() + f'\nX-AppImage-Version={version}\n')
        _run(['appimagetool', '--updateinformation=gh-releases-zsync|keepassxreboot|keepassxc|latest|' +
              f'KeePassXC-*-{platform_target}.AppImage.zsync',
              app_dir.as_posix(), (output_dir.absolute() / appimage_name).as_posix()],
             cwd=build_dir, capture_output=False, path=env_path, **docker_args, docker_privileged=True)


class AppSign(Command):
    """Sign binaries with code signing certificates on Windows and macOS."""

    @classmethod
    def setup_arg_parser(cls, parser: argparse.ArgumentParser):
        parser.add_argument('file', help='Input file(s) to sign.', nargs='+')
        parser.add_argument('-i', '--identity', help='Key or identity used for the signature (default: ask).')
        parser.add_argument('-s', '--src-dir', help='Source directory (default: %(default)s).', default='.')

        if sys.platform == 'darwin':
            parser.add_argument('-n', '--notarize', help='Notarize signed file(s).', action='store_true')
            parser.add_argument('-c', '--keychain-profile', default='notarization-creds',
                                help='Read Apple credentials for notarization from a keychain (default: %(default)s).')

    def run(self, file, identity, src_dir, **kwargs):
        for i, f in enumerate(file):
            f = Path(f)
            if not f.exists():
                raise Error('Input file does not exist: %s', f)
            file[i] = f

        if sys.platform == 'win32':
            for f in file:
                self.sign_windows(f, identity, Path(src_dir))

        elif sys.platform == 'darwin':
            if kwargs['notarize']:
                self._macos_validate_keychain_profile(kwargs['keychain_profile'])
            identity = self._macos_get_codesigning_identity(identity)
            for f in file:
                out_file = self.sign_macos(f, identity, Path(src_dir))
                if kwargs['notarize'] and out_file.suffix == '.dmg':
                    self.notarize_macos(out_file, kwargs['keychain_profile'])

        else:
            raise Error('Unsupported platform.')

    def sign_windows(self, file, identity, src_dir):
        pass

    # noinspection PyMethodMayBeStatic
    def _macos_validate_keychain_profile(self, keychain_profile):
        if _run(['xcrun', 'security', 'find-generic-password', '-a',
                 f'com.apple.gke.notary.tool.saved-creds.{keychain_profile}'], cwd=None, check=False).returncode != 0:
            raise Error(f'Keychain profile "%s" not found! Run\n'
                        f'    {_TERM_BOLD}xcrun notarytool store-credentials %s [...]{_TERM_RES_BOLD}\n'
                        f'to store your Apple notary service credentials in a keychain as "%s".',
                        keychain_profile, keychain_profile, keychain_profile)

    # noinspection PyMethodMayBeStatic
    def _macos_get_codesigning_identity(self, user_choice=None):
        result = _run(['xcrun', 'security', 'find-identity', '-v', '-p', 'codesigning'],
                      check=False, cwd=None, text=True)
        if result.returncode != 0:
            return []
        identities = [l.strip() for l in result.stdout.strip().split('\n')[:-1]]
        identities = [i.split(' ', 2)[1:] for i in identities]
        if not identities:
            raise Error('No codesigning identities found.')

        if not user_choice and len(identities) == 1:
            identity = identities[0][0]
            logger.info('Using codesigning identity %s.', identities[0][1])
        elif not user_choice:
            c = _choice_prompt(
                'The following code signing identities were found. Which one do you want to use?',
                [' '.join(i) for i in identities])
            identity = identities[c][0]
        else:
            for i in identities:
                # Exact match of ID or substring match of description
                if user_choice == i[0] or user_choice in i[1]:
                    identity = i[0]
                    break
            else:
                raise Error('Invalid identity: %s', user_choice)

        return identity

    # noinspection PyMethodMayBeStatic
    def sign_macos(self, file, identity, src_dir):
        logger.info('Signing file: "%s"', file)

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp).absolute()
            app_dir = tmp / 'app'
            out_file = file.parent / file.name.replace('-unsigned', '')

            if file.is_file() and file.suffix == '.dmg':
                logger.debug('Unpacking disk image...')
                mnt = tmp / 'mnt'
                mnt.mkdir()
                try:
                    _run(['hdiutil', 'attach', '-noautoopen', '-mountpoint', mnt.as_posix(), file.as_posix()], cwd=None)
                    shutil.copytree(mnt, app_dir, symlinks=True)
                finally:
                    _run(['hdiutil', 'detach', mnt.as_posix()], cwd=None)
            elif file.is_dir() and file.suffix == '.app':
                logger.debug('Copying .app directory...')
                shutil.copytree(file, app_dir, symlinks=True)
            else:
                logger.warning('Skipping non-app file "%s"', file)
                return

            app_dir_app = list(app_dir.glob('*.app'))[0]

            logger.debug('Signing libraries and frameworks...')
            _run(['xcrun', 'codesign', f'--sign={identity}', '--force', '--options=runtime', '--deep',
                  app_dir_app.as_posix()], cwd=None)

            # (Re-)Sign main executable with --entitlements
            logger.debug('Signing main executable...')
            _run(['xcrun', 'codesign', f'--sign={identity}', '--force', '--options=runtime',
                  '--entitlements', (src_dir / 'share/macosx/keepassxc.entitlements').as_posix(),
                  (app_dir_app / 'Contents/MacOS/KeePassXC').as_posix()], cwd=None)

            tmp_out = out_file.with_suffix(f'.{"".join(random.choices(string.ascii_letters, k=8))}{file.suffix}')
            try:
                if file.suffix == '.dmg':
                    logger.debug('Repackaging disk image...')
                    dmg_size = sum(f.stat().st_size for f in app_dir.rglob('*'))
                    _run(['hdiutil', 'create', '-volname', 'KeePassXC', '-srcfolder', app_dir.as_posix(),
                          '-fs', 'HFS+', '-fsargs', '-c c=64,a=16,e=16', '-format', 'UDBZ',
                          '-size', f'{dmg_size}k', tmp_out.as_posix()],
                    cwd=None)
                elif file.suffix == '.app':
                    shutil.copytree(app_dir, tmp_out, symlinks=True)
            except:
                if tmp_out.is_file():
                    tmp_out.unlink()
                elif tmp_out.is_dir():
                    shutil.rmtree(tmp_out, ignore_errors=True)
                raise
            finally:
                # Replace original file if all went well
                if tmp_out.exists():
                    if tmp_out.is_dir():
                        shutil.rmtree(file)
                    else:
                        file.unlink()
                    tmp_out.rename(out_file)

        logger.info('File signed successfully and written to: "%s".', out_file)
        return out_file

    # noinspection PyMethodMayBeStatic
    def notarize_macos(self, file, keychain_profile):
        logger.info('Submitting "%s" for notarization...', file)
        _run(['xcrun', 'notarytool', 'submit', f'--keychain-profile={keychain_profile}', '--wait',
              file.as_posix()], cwd=None, capture_output=False)

        logger.debug('Stapling notarization ticket...')
        _run(['xcrun', 'stapler', 'staple', file.as_posix()], cwd=None)
        _run(['xcrun', 'stapler', 'validate', file.as_posix()], cwd=None)

        logger.info('Notarization successful.')


class GPGSign(Command):
    """Sign previously compiled release packages with GPG."""

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

        subparsers = parser.add_subparsers(title='Subcommands', dest='subcmd')
        push = subparsers.add_parser('tx-push', help='Push source translation file to Transifex.')
        push.add_argument('-r', '--resource', help='Transifex resource name.', choices=['master', 'develop'])
        push.add_argument('-y', '--yes', help='Don\'t ask before pushing source file.', action='store_true')
        push.add_argument('tx_args', help='Additional arguments to pass to tx subcommand.', nargs=argparse.REMAINDER)

        pull = subparsers.add_parser('tx-pull', help='Pull updated translations from Transifex.')
        pull.add_argument('-r', '--resource', help='Transifex resource name.', choices=['master', 'develop'])
        pull.add_argument('-m', '--min-perc', help='Minimum percent complete for pull (default: %(default)s).',
                          choices=range(0, 101), metavar='[0-100]', default=TRANSIFEX_PULL_PERC)
        pull.add_argument('-c', '--commit', help='Commit changes.', action='store_true')
        pull.add_argument('-y', '--yes', help='Don\'t ask before pulling translations.', action='store_true')
        pull.add_argument('tx_args', help='Additional arguments to pass to tx subcommand.', nargs=argparse.REMAINDER)

        lupdate = subparsers.add_parser('lupdate', help='Update source translation file from C++ sources.')
        lupdate.add_argument('-d', '--build-dir', help='Build directory for looking up lupdate binary.')
        lupdate.add_argument('-c', '--commit', help='Commit changes.', action='store_true')
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
            result = _run(['lupdate', '-version'], path=path, check=False, cwd=None, text=True)
            if result.returncode == 0 and result.stdout.startswith('lupdate version 5.'):
                return
        raise Error('lupdate command not found. Make sure it is installed and the correct version.')

    def run(self, subcmd, src_dir, branch, **kwargs):
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

            kwargs['resource'] = self.derive_resource_name(kwargs['resource'], cwd=src_dir)
            kwargs['resource'] = TRANSIFEX_RESOURCE.format(kwargs['resource'])
            kwargs['tx_args'] = kwargs['tx_args'][1:]
            if subcmd == 'tx-push':
                self.run_tx_push(src_dir, **kwargs)
            elif subcmd == 'tx-pull':
                self.run_tx_pull(src_dir, **kwargs)

        elif subcmd == 'lupdate':
            kwargs['lupdate_args'] = kwargs['lupdate_args'][1:]
            self.run_lupdate(src_dir, **kwargs)

    # noinspection PyMethodMayBeStatic
    def derive_resource_name(self, override_resource=None, *, cwd):
        if override_resource:
            res = override_resource
        elif _git_branches_related('develop', 'HEAD', cwd=cwd):
            logger.info(f'Branch derives from develop, using {_TERM_BOLD}"develop"{_TERM_RES_BOLD} resource.')
            res = 'develop'
        else:
            logger.info(f'Release branch, using {_TERM_BOLD}"master"{_TERM_RES_BOLD} resource.')
            res = 'master'
        return TRANSIFEX_RESOURCE.format(res)

    # noinspection PyMethodMayBeStatic
    def run_tx_push(self, src_dir, resource, yes, tx_args):
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

    # noinspection PyMethodMayBeStatic
    def run_tx_pull(self, src_dir, resource, min_perc, commit=False, yes=False, tx_args=None):
        sys.stderr.write(f'\nAbout to pull translations for {_TERM_BOLD}"{resource}"{_TERM_RES_BOLD}.\n')
        if not yes and not _yes_no_prompt('Continue?'):
            logger.error('Pull aborted.')
            return
        logger.info('Pulling translations from Transifex...')
        tx_args = tx_args or []
        _run(['tx', 'pull', '--all', '--use-git-timestamps', f'--minimum-perc={min_perc}', *tx_args, resource],
             cwd=src_dir, capture_output=False)
        logger.info('Pull successful.')
        files = [f.relative_to(src_dir) for f in Path(src_dir).glob('share/translations/*.ts')]
        if commit:
            _git_commit_files(files, 'Update translations.', cwd=src_dir)

    def run_lupdate(self, src_dir, build_dir=None, commit=False, lupdate_args=None):
        path = _get_bin_path(build_dir)
        self.check_lupdate_exists(path)
        logger.info('Updating translation source files from C++ sources...')
        _run(['lupdate', '-no-ui-lines', '-disable-heuristic', 'similartext', '-locations', 'none',
              '-extensions', 'c,cpp,h,js,mm,qrc,ui', '-no-obsolete', 'src',
              '-ts', str(Path(f'share/translations/keepassxc_en.ts')), *(lupdate_args or [])],
             cwd=src_dir, path=path, capture_output=False)
        logger.info('Translation source files updated.')
        if commit:
            _git_commit_files([f'share/translations/keepassxc_en.ts'],
                              'Update translation sources.', cwd=src_dir)


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

    appsign_parser = subparsers.add_parser('appsign', help=AppSign.__doc__)
    AppSign.setup_arg_parser(appsign_parser)
    appsign_parser.set_defaults(_cmd=AppSign)

    gpgsign_parser = subparsers.add_parser('gpgsign', help=GPGSign.__doc__)
    Merge.setup_arg_parser(gpgsign_parser)
    gpgsign_parser.set_defaults(_cmd=GPGSign)

    i18n_parser = subparsers.add_parser('i18n', help=I18N.__doc__)
    I18N.setup_arg_parser(i18n_parser)
    i18n_parser.set_defaults(_cmd=I18N)

    args = parser.parse_args()
    if '_cmd' not in args:
        parser.print_help()
        return 1
    return args._cmd(parser).run(**{k: v for k, v in vars(args).items() if k != '_cmd'}) or 0


def _sig_handler(_, __):
    logger.error('Process interrupted.')
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
        logger.error('Process interrupted.')
        ret = 3
    except Exception as e:
        logger.critical('Unhandled exception:', exc_info=e)
        ret = 4
    except SystemExit as e:
        ret = e.code
    finally:
        ret |= _cleanup()
        sys.exit(ret)
