#!/usr/bin/env python3

import os
import sys
import time
import base64
import atexit
import gc
import html
import urllib.parse
import subprocess
import warnings
import random
import json
import logging
import asyncio
import argparse
from importlib.metadata import version as get_version, PackageNotFoundError
import platform
import aiofiles
import re
import aiohttp
from packaging.version import parse as parse_version
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, quote
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from colorama import init, Fore, Style
from tqdm.asyncio import tqdm_asyncio

init(autoreset=True)

MAX_CONCURRENT_WORKERS = 30
DEFAULT_WORKERS = 10
PAYLOADS_BATCH_SIZE = 15
URLS_BATCH_SIZE = 5
MAX_WORKERS_PER_BATCH = 10
DEFAULT_RATE_LIMIT = 12
DEFAULT_TIMEOUT = 8
DEFAULT_ALERT_TIMEOUT = 6
CONNECTIONS_PER_WORKER = 3
MIN_RATE_LIMIT = 1
MAX_RATE_LIMIT = 100
ERROR_LOG_FILE = "logs/errors.log"

# Telegram Configuration
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
TELEGRAM_NOTIFICATIONS_ENABLED = False

VERSION = "0.0.1"
GITHUB_REPOSITORY: str = "Cybersecurity-Ethical-Hacker/xssuccessor"
GITHUB_URL: str = f"https://github.com/{GITHUB_REPOSITORY}"

class CustomStderr:
    def __init__(self, error_log_path):
        self.error_log = open(error_log_path, 'a')
        self.stderr = sys.stderr
        self.isatty = getattr(sys.stderr, 'isatty', lambda: False)

    def write(self, message):
        if any(pattern in message for pattern in [
            'Event loop is closed',
            'Task was destroyed but it is pending',
            'Exception ignored in:',
            'task: <Task cancelled',
            'BaseSubprocessTransport.__del__',
            'transport was deleted'
        ]):
            self.error_log.write(message)
            self.error_log.flush()
        else:
            self.stderr.write(message)
            self.stderr.flush()

    def flush(self):
        self.error_log.flush()
        self.stderr.flush()

    def close(self):
        self.error_log.close()

    def isatty(self):
        return self.stderr.isatty()

    def fileno(self):
        return self.stderr.fileno()

def cleanup_stderr():
    try:
        if hasattr(sys.stderr, 'close'):
            sys.stderr.close()
    except:
        pass

atexit.register(cleanup_stderr)

warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", category=ResourceWarning)
warnings.filterwarnings("ignore", message="Event loop is closed")
warnings.filterwarnings("ignore", message=".*Task .* was destroyed but it is pending!.*")

def custom_exception_handler(loop, context):
    if isinstance(context.get('exception'), RuntimeError):
        if 'Event loop is closed' in str(context.get('message', '')):
            return
    if isinstance(context.get('exception'), asyncio.CancelledError):
        return
    msg = context.get('message')
    if not any(ignore in str(msg) for ignore in [
        'Event loop is closed',
        'Task was destroyed but it is pending',
        'task: <Task cancelled',
        'transport was deleted'
    ]):
        pass

def cleanup_loop():
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.stop()
        if not loop.is_closed():
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
    except:
        pass

def cleanup_resources():
    gc.collect()

atexit.register(cleanup_resources)
atexit.register(cleanup_loop)

loop = asyncio.new_event_loop()
loop.set_exception_handler(custom_exception_handler)
asyncio.set_event_loop(loop)

def log_error(message: str, exc: Optional[Exception] = None) -> None:
    if exc:
        logging.error(message, exc_info=True)
    else:
        logging.error(message)

def get_playwright_version() -> str:
    try:
        import playwright
        version = getattr(playwright, '__version__', None)
        if not version:
            try:
                version = get_version('playwright')
            except PackageNotFoundError:
                version = "unknown"
        if version == "0.0.0":
            version = "0.0.0 (Normal on some Linux distributions)"
        return version
    except Exception as e:
        return f"error retrieving version: {e}"

def check_playwright_version() -> str:
    playwright_version = get_playwright_version()
    try:
        if playwright_version == "0.0.0" or playwright_version == "0.0.0 (Normal on some Linux distributions)":
            return playwright_version
        required_version = '1.35.0'
        if parse_version(playwright_version) < parse_version(required_version):
            print(f"{Fore.RED}Error: This tool requires Playwright >= {required_version}")
            print(f"Current version: {playwright_version}")
            print(f"Please upgrade: pip install -U playwright{Style.RESET_ALL}")
            sys.exit(1)
    except Exception as e:
        if "not installed" in str(e):
            print(f"{Fore.RED}Error: Playwright is not installed")
            print("Please install: pip install playwright>=1.35.0")
            print(f"Then run: playwright install chromium{Style.RESET_ALL}")
            sys.exit(1)
        else:
            print(f"{Fore.RED}Error checking Playwright version: {e}{Style.RESET_ALL}")
            sys.exit(1)
    return playwright_version

class DOMXSSScanner:
    def __init__(self):
        self.dom_patterns = {
            'sources': re.compile(r'(?:document\.URL|location\.(?:href|search|hash)|document\.(?:referrer|cookie)|localStorage)',
                                re.IGNORECASE),
            'sinks': re.compile(r'(?:eval|innerHTML|outerHTML|document\.write|setTimeout|setInterval)',
                              re.IGNORECASE)
        }

    async def quick_dom_check(self, response_text: str) -> bool:
        """Quick initial check for DOM-based XSS potential without browser"""
        # Check for innerHTML assignments to specific elements
        if re.search(r'getElementById\([\'"]greeting[\'"]\)\.innerHTML', response_text, re.I):
            return True

        script_tags = re.findall(r'<script[^>]*>(.*?)</script>', response_text, re.DOTALL)
        for script in script_tags:
            # Check for URL parameter access
            has_param_access = re.search(r'URLSearchParams|params\.get|location\.search', script, re.I)
            # Check for DOM modification
            has_dom_mod = re.search(r'innerHTML\s*=|document\.write', script, re.I)
            
            if has_param_access and has_dom_mod:
                return True
            
            # Check for vulnerable patterns
            if re.search(r'getElementById\([^)]+\)\.innerHTML\s*=', script, re.I):
                return True

            if 'window.onload' in script and 'innerHTML' in script:
                return True

        return False

class TimeoutFilter(logging.Filter):
    TIMEOUT_PATTERNS = [
        "Timeout",
        "TimeoutError",
        "Page.goto: Timeout",
        "page.goto: Timeout",
        "Call log:",
        "navigating to",
        "Error checking reflection",
        "Error closing browser",
        "Connection closed"
    ]
    def filter(self, record: logging.LogRecord) -> bool:
        return not any(pattern in str(record.msg) for pattern in self.TIMEOUT_PATTERNS)

class VersionManager:
    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.version_pattern = re.compile(r'VERSION\s*=\s*["\']([0-9]+\.[0-9]+\.[0-9]+)["\']')

    def get_current_version(self) -> str:
        try:
            content = self.file_path.read_text()
            match = self.version_pattern.search(content)
            if match:
                return match.group(1)
            raise ValueError("VERSION variable not found in file")
        except Exception as e:
            log_error(f"Error reading version: {e}")
            return "0.0.1"

    def update_version(self, new_version: str) -> bool:
        try:
            content = self.file_path.read_text()
            updated_content = self.version_pattern.sub(f'VERSION = "{new_version}"', content)
            self.file_path.write_text(updated_content)
            return True
        except Exception as e:
            log_error(f"Error updating version: {e}")
            return False

class Updater:
    def __init__(self) -> None:
        self.current_version: str = VERSION
        self.repo_path: Path = Path(__file__).parent
        self.is_git_repo: bool = self._check_git_repo()
        self.default_branch: Optional[str] = self._detect_default_branch()

    def _check_git_repo(self) -> bool:
        try:
            subprocess.run(
                ['git', 'rev-parse', '--git-dir'],
                cwd=self.repo_path,
                capture_output=True,
                check=True
            )
            return True
        except subprocess.CalledProcessError:
            return False
        except Exception as e:
            return False

    def _detect_default_branch(self) -> Optional[str]:
        if not self.is_git_repo:
            return None
        try:
            result = subprocess.run(
                ['git', 'remote', 'show', 'origin'],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=True
            )
            for line in result.stdout.split('\n'):
                if 'HEAD branch:' in line:
                    return line.split(':')[1].strip()
        except:
            pass
        return 'main'

class AutoUpdater(Updater):
    def _check_git_repo(self) -> bool:
        try:
            env = os.environ.copy()
            env["GIT_ASKPASS"] = "echo"
            env["GIT_TERMINAL_PROMPT"] = "0"
            with open(os.devnull, 'w') as devnull:
                subprocess.run(
                    ['git', 'rev-parse', '--git-dir'],
                    stdout=subprocess.PIPE,
                    stderr=devnull,
                    text=True,
                    check=True,
                    timeout=2,
                    env=env,
                    cwd=self.repo_path
                )
            return True
        except:
            return False

    def _get_local_version(self) -> str:
        try:
            env = os.environ.copy()
            env["GIT_ASKPASS"] = "echo"
            env["GIT_TERMINAL_PROMPT"] = "0"
            result = subprocess.run(
                ['git', 'describe', '--tags', '--abbrev=0'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=2,
                env=env,
                cwd=self.repo_path
            )
            if result.returncode == 0:
                return result.stdout.strip().lstrip('v')
            return self.current_version
        except:
            return self.current_version

    def _detect_default_branch(self) -> Optional[str]:
        if not self.is_git_repo:
            return None
        try:
            env = os.environ.copy()
            env["GIT_ASKPASS"] = "echo"
            env["GIT_TERMINAL_PROMPT"] = "0"
            with open(os.devnull, 'w') as devnull:
                result = subprocess.run(
                    ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                    stdout=subprocess.PIPE,
                    stderr=devnull,
                    text=True,
                    check=True,
                    timeout=2,
                    env=env,
                    cwd=self.repo_path
                )
                return result.stdout.strip() or 'main'
        except:
            return 'main'

    def _get_remote_changes(self) -> Tuple[bool, str]:
        if not self.default_branch:
            return False, "Check skipped"
        env = os.environ.copy()
        env["GIT_ASKPASS"] = "echo"
        env["GIT_TERMINAL_PROMPT"] = "0"
        local_version = self._get_local_version()
        try:
            with open(os.devnull, 'w') as devnull:
                fetch_result = subprocess.run(
                    ['git', 'fetch', '--tags', 'origin'],
                    stdout=subprocess.PIPE,
                    stderr=devnull,
                    text=True,
                    timeout=2,
                    env=env,
                    cwd=self.repo_path
                )
                if fetch_result.returncode != 0:
                    return False, "Check skipped"
                result = subprocess.run(
                    ['git', 'describe', '--tags', '--abbrev=0', f'origin/{self.default_branch}'],
                    stdout=subprocess.PIPE,
                    stderr=devnull,
                    text=True,
                    timeout=2,
                    env=env,
                    cwd=self.repo_path
                )
                if result.returncode != 0:
                    return False, "Check skipped"
                remote_tag = result.stdout.strip()
                if not remote_tag:
                    return False, "Check skipped"
                remote_version = remote_tag.lstrip('v')
                if self._compare_versions(remote_version, local_version):
                    return True, remote_version
                else:
                    return False, local_version
        except subprocess.TimeoutExpired:
            return False, "Check skipped"
        except Exception as e:
            return False, "Check skipped"

    def _run_git_command(self, command: List[str]) -> Optional[str]:
        if not self.is_git_repo:
            return None
        try:
            env = os.environ.copy()
            env["GIT_ASKPASS"] = "echo"
            env["GIT_TERMINAL_PROMPT"] = "0"
            with open(os.devnull, 'w') as devnull:
                result = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=devnull,
                    text=True,
                    check=True,
                    timeout=2,
                    env=env,
                    cwd=self.repo_path
                )
            return result.stdout.strip()
        except:
            return None

    def _perform_update(self) -> Dict[str, Any]:
        if not self.default_branch:
            return {
                'status': 'error',
                'message': 'No default branch detected'
            }
        if not self._run_git_command(['git', 'reset', '--hard', f'origin/{self.default_branch}']):
            return {
                'status': 'error',
                'message': 'Update failed'
            }
        pull_output = self._run_git_command(['git', 'pull', '--force', 'origin', self.default_branch])
        if not pull_output:
            return {
                'status': 'error',
                'message': 'Pull failed'
            }
        current_tag = self._run_git_command(['git', 'describe', '--tags', '--abbrev=0']) or self.current_version
        return {
            'status': 'success',
            'message': 'Update successful',
            'version': current_tag.lstrip('v'),
            'changes': pull_output,
            'updated': True
        }

    def _compare_versions(self, v1: str, v2: str) -> bool:
        def to_ints(v: str):
            return list(map(int, v.split('.')))
        result = to_ints(v1) > to_ints(v2)
        return result

    def check_and_update(self) -> Dict[str, Any]:
        if not self.is_git_repo:
            return {
                'status': 'error',
                'message': 'Not a git repository'
            }
        has_changes, info = self._get_remote_changes()
        if info == "Check skipped":
            return {
                'status': 'success',
                'message': 'Check skipped',
                'version': self.current_version,
                'updated': False
            }
        elif not has_changes:
            return {
                'status': 'success',
                'message': 'Already at latest version',
                'version': self.current_version,
                'updated': False
            }
        update_result = self._perform_update()
        return update_result

class VersionInfo:
    def __init__(self, current: str, update_available: str):
        self.current = current
        self.update_available = update_available

class CustomHelpFormatter(argparse.HelpFormatter):
    def _format_action_invocation(self, action: argparse.Action) -> str:
        if not action.option_strings:
            metavar, = self._metavar_formatter(action, action.dest)(1)
            return metavar
        else:
            parts = []
            if hasattr(self, '_usage_mode'):
                return action.option_strings[0]
            if action.nargs == 0:
                parts.extend(action.option_strings)
            else:
                parts.extend(action.option_strings)
            return ', '.join(parts)

    def _expand_help(self, action: argparse.Action) -> str:
        params = dict(vars(action), prog=self._prog)
        for name in list(params):
            if params[name] is argparse.SUPPRESS:
                del params[name]
            elif hasattr(params[name], '__name__'):
                params[name] = params[name].__name__
        if params.get('help') == 'show this help message and exit':
            return 'Show this help message and exit'
        return self._get_help_string(action) % params

    def _format_usage(self, usage: Optional[str], actions: List[argparse.Action],
                     groups: List[argparse._ArgumentGroup], prefix: Optional[str]) -> str:
        if prefix is None:
            prefix = 'usage: '
        self._usage_mode = True
        action_usage = []
        action_usage.append("[-h HELP]")
        action_usage.append("[-d DOMAIN | -l URL_LIST]")
        for action in actions:
            if action.option_strings:
                if action.option_strings[0] not in ['-h', '-d', '-l']:
                    msg = self._format_action_invocation(action)
                    upper_dest = action.dest.upper()
                    action_usage.append(f"[{msg} {upper_dest}]")
        usage = ' '.join([x for x in action_usage if x])
        delattr(self, '_usage_mode')
        return f"{prefix}{self._prog} {usage}\n\n"

class CustomArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        args = sys.argv[1:]
        if len(args) == 0:
            self.print_help()
            sys.exit(2)
        if '-u' in args or '--update' in args:
            if len(args) == 1:
                return
        self.print_help()
        print(f"\n{Fore.RED}Error: {message}{Style.RESET_ALL}")
        if "one of the arguments -d/--domain -l/--url-list" in message:
            print(f"\n{Fore.RED}❌ One of the arguments is required -d/--domain or -l/--url-list{Style.RESET_ALL}")
        elif "Please provide a full URL with scheme" in message:
            print(f"\n{Fore.RED}❌ Error: Invalid URL format.{Style.RESET_ALL}")
            print(f"{Fore.YELLOW}🧩 URL must start with http:// or https://{Style.RESET_ALL}")
            print(f"{Fore.CYAN}🔗 Example of a valid URL: https://example.com/page?param1=value&param2=test{Style.RESET_ALL}")
        elif "at least one query parameter" in message:
            print(f"\n{Fore.RED}❌ Error: Invalid URL format.{Style.RESET_ALL}")
            print(f"{Fore.YELLOW}🧩 URL must include at least one query parameter.{Style.RESET_ALL}")
            print(f"{Fore.CYAN}🔗 Example of a valid URL: https://example.com/page?param1=value&param2=test{Style.RESET_ALL}")
        elif "unrecognized arguments" in message:
            print(f"{Fore.YELLOW}Use -h or --help to see available options{Style.RESET_ALL}")
        sys.exit(2)

def validate_url(url: str) -> bool:
    """
    Validate URL for parameter presence in both normal queries and after fragments.
    Now accepts both empty parameters and parameters with values.
    """
    try:
        # First decode any URL encoding
        decoded_url = urllib.parse.unquote(url)
        
        # Check for basic URL structure
        if not decoded_url.startswith(('http://', 'https://')):
            return False
            
        # Look for parameters either in normal query or after fragment
        if '?' not in decoded_url:
            return False
            
        # Check if there's at least one parameter with a name
        query_part = ""
        if '#' in decoded_url and '?' in decoded_url.split('#')[1]:
            # Parameters are after the fragment
            query_part = decoded_url.split('#')[1].split('?')[1]
        else:
            # Normal query parameters
            query_part = decoded_url.split('?')[1]
            
        # Check for any valid parameter (with or without value)
        params = query_part.split('&')
        for param in params:
            if param and '=' in param:
                param_name = param.split('=')[0]
                if param_name:  # Only check if parameter has a name
                    return True
        return False
        
    except Exception:
        return False

def parse_arguments() -> argparse.Namespace:
    parser = CustomArgumentParser(
        formatter_class=lambda prog: CustomHelpFormatter(prog, max_help_position=80)
    )
    parser.add_argument('-u', '--update',
                      action='store_true',
                      help='Check for updates and automatically install the latest version')
    mutex_group = parser.add_mutually_exclusive_group(required=False)
    mutex_group.add_argument('-d', '--domain',
                           help='Specify the domain with parameter(s) to scan (required unless -l is used)')
    mutex_group.add_argument('-l', '--url-list',
                           help='Provide a file containing a list of URLs with parameters to scan')
    parser.add_argument('-p', '--payloads', default='xss_payloads.txt',
                       help='Custom file containing payloads')
    parser.add_argument('-o', '--output',
                       help='Specify the output file name (supports .txt or .json)')
    parser.add_argument('-w', '--workers', type=int, default=DEFAULT_WORKERS,
                       help=f'Maximum number of concurrent workers')
    parser.add_argument('-t', '--timeout', type=int, default=DEFAULT_TIMEOUT,
                       help=f'Total request timeout in seconds')
    parser.add_argument('-a', '--alert-timeout', type=int, default=DEFAULT_ALERT_TIMEOUT,
                       help=f'Specify the alert timeout in seconds')
    parser.add_argument('-j', '--json', action='store_true',
                       help='Output results in JSON format')
    parser.add_argument('-H', '--header', action='append',
                       help='Custom headers can be specified multiple times. Format: "Header: Value"')
    parser.add_argument('-b', '--batch-size', type=int, default=PAYLOADS_BATCH_SIZE,
                       help=f'Define the number of requests per batch (default: {PAYLOADS_BATCH_SIZE})')
    parser.add_argument('-r', '--rate-limit', type=int, default=DEFAULT_RATE_LIMIT,
                       help=f'Maximum number of requests per second (default: {DEFAULT_RATE_LIMIT})')
    args = parser.parse_args()
    
    if not args.update and not (args.domain or args.url_list):
        parser.error("One of the arguments -d/--domain -l/--url-list is required")
    
    if not args.update:
        if args.workers < 1 or args.workers > MAX_CONCURRENT_WORKERS:
            parser.error(f"Workers must be between 1 and {MAX_CONCURRENT_WORKERS}")
        if args.timeout < 1 or args.timeout > 60:
            parser.error("Timeout must be between 1 and 60 seconds")
        if args.alert_timeout < 1 or args.alert_timeout > 30:
            parser.error("Alert timeout must be between 1 and 30 seconds")
        if args.batch_size < 1 or args.batch_size > 1000:
            parser.error("Batch size must be between 1 and 1000")
        if args.rate_limit < MIN_RATE_LIMIT or args.rate_limit > MAX_RATE_LIMIT:
            parser.error(f"Rate limit must be between {MIN_RATE_LIMIT} and {MAX_RATE_LIMIT} requests per second")
        if args.domain:
            if not validate_url(args.domain):
                parser.error("\n\nPlease provide a valid URL with parameters. Examples:\n" +
                           "- http://testhtml5.vulnweb.com/#/redir?url=\n" +
                           "- http://testhtml5.vulnweb.com/%23/redir?url=value\n" +
                           "- https://example.com/page?param1=value")
    return args

class GitHandler:
    @staticmethod
    def check_git() -> Tuple[bool, str]:
        try:
            result = subprocess.run(
                ['git', '--version'],
                capture_output=True,
                text=True,
                check=True,
                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
                stdin=subprocess.DEVNULL
            )
            return True, result.stdout.strip()
        except FileNotFoundError:
            return False, "Git is not installed"
        except subprocess.CalledProcessError as e:
            return False, f"Git error: {e.stderr.strip()}"
        except Exception as e:
            return False, str(e)

    @staticmethod
    def check_repo_status() -> Tuple[bool, str]:
        try:
            subprocess.run(
                ['git', 'rev-parse', '--git-dir'],
                capture_output=True,
                check=True,
                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
                stdin=subprocess.DEVNULL
            )
            return True, "Repository OK"
        except subprocess.CalledProcessError:
            return False, "Update: Repository not initialized"
        except Exception as e:
            return False, f"Update: Repository connection error"

    @staticmethod
    def get_installation_instructions() -> str:
        system = platform.system().lower()
        if system == "windows":
            return """
Git is not installed. To install Git on Windows:

1. Download the official Git installer:
   https://git-scm.com/download/windows

2. Or install with winget:
   winget install --id Git.Git -e --source winget

3. Or install with Chocolatey:
   choco install git

After installation, restart your terminal/command prompt.
"""
        elif system == "darwin":
            return """
Git is not installed. To install Git on macOS:

1. Install with Homebrew (recommended):
   brew install git

2. Install Xcode Command Line Tools (alternative):
   xcode-select --install

After installation, restart your terminal.
"""
        elif system == "linux":
            try:
                with open('/etc/os-release') as f:
                    distro = f.read().lower()
                if 'ubuntu' in distro or 'debian' in distro or 'kali' in distro:
                    return """
Git is not installed. To install Git:

1. Update package list:
   sudo apt update

2. Install git:
   sudo apt install git

After installation, restart your terminal.
"""
                elif 'fedora' in distro or 'rhel' in distro or 'centos' in distro:
                    return """
Git is not installed. To install Git:

1. Install git:
   sudo dnf install git  (Fedora)
   sudo yum install git  (RHEL/CentOS)

After installation, restart your terminal.
"""
                elif 'arch' in distro:
                    return """
Git is not installed. To install Git:

1. Install git:
   sudo pacman -S git

After installation, restart your terminal.
"""
            except:
                pass
            return """
Git is not installed. To install Git on Linux:
Common commands:
- Ubuntu/Debian/Kali: sudo apt install git
- Fedora: sudo dnf install git
- Arch: sudo pacman -S git

After installation, restart your terminal.
"""
        return """
Git is not installed. Please install Git for your operating system:
https://git-scm.com/downloads
"""

    def ensure_git_available(self) -> bool:
        is_installed, message = self.check_git()
        if not is_installed:
            print(f"\n{Fore.RED}Error: {message}{Style.RESET_ALL}")
            print(f"\n{Fore.YELLOW}Installation Instructions:{Style.RESET_ALL}")
            print(self.get_installation_instructions())
            return False
        return True

class Config:
    def __init__(self, args: argparse.Namespace, playwright_version: str) -> None:
        self.github_url = "https://github.com/Cybersecurity-Ethical-Hacker/xssuccessor"
        self.github_repository = "Cybersecurity-Ethical-Hacker/xssuccessor"
        updater = AutoUpdater()
        self.current_version = updater._get_local_version()
        self.domain: Optional[str] = args.domain
        self.url_list: Optional[str] = args.url_list
        self.json_output: bool = args.json
        self.timeout: int = args.timeout
        self.alert_timeout: int = args.alert_timeout
        self.max_workers: int = args.workers
        self.batch_size: int = min(args.batch_size, PAYLOADS_BATCH_SIZE)
        self.rate_limit: int = args.rate_limit  # Add rate limit config
        self.playwright_version: str = playwright_version
        
        # Git handling
        git_handler = GitHandler()
        repo_status, repo_message = git_handler.check_repo_status()
        self.version_info: VersionInfo = self._check_version()
        
        # Header handling
        self.custom_headers_present = False
        custom_headers = {}
        if args.header:
            self.custom_headers_present = True
            for header in args.header:
                if ':' in header:
                    key, value = header.split(':', 1)
                    custom_headers[key.strip()] = value.strip()
                else:
                    print(f"{Fore.RED}Invalid header format: {header}. Headers must be in 'HeaderName: HeaderValue' format.{Style.RESET_ALL}")
                    sys.exit(1)
        self.headers = HeaderManager.get_headers(custom_headers)
        
        # Setup
        self._setup_directories(args)
        self._setup_files(args)

    def _check_version(self) -> VersionInfo:
        try:
            git_handler = GitHandler()
            repo_status, repo_message = git_handler.check_repo_status()
            if not repo_status:
                return VersionInfo(
                    current=VERSION,
                    update_available='Unknown (No Repository)'
                )
            updater = AutoUpdater()
            local_version = updater._get_local_version()
            has_changes, info = updater._get_remote_changes()
            if info == "Check skipped":
                return VersionInfo(
                    current=local_version,
                    update_available='Check skipped'
                )
            elif has_changes:
                return VersionInfo(
                    current=local_version,
                    update_available='Yes'
                )
            return VersionInfo(
                current=local_version,
                update_available='No'
            )
        except Exception as e:
            log_error(f"Update check: {str(e)}")
            return VersionInfo(
                current=VERSION,
                update_available='Check Failed'
            )

    def _setup_directories(self, args: argparse.Namespace) -> None:
        if self.domain:
            parsed_url = urlparse(self.domain)
            base_name = parsed_url.netloc.split(':')[0]
        elif self.url_list:
            base_name = Path(self.url_list).stem
        else:
            base_name = f"scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.base_dir = Path(f"scans/{base_name}")
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _setup_files(self, args: argparse.Namespace) -> None:
        self.payload_file = Path(args.payloads)
        if not self.payload_file.exists():
            print(f"{Fore.RED}\nError: Payload file not found: {self.payload_file}{Style.RESET_ALL}")
            sys.exit(1)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if args.output:
            output_path = Path(args.output)
            if self.json_output and output_path.suffix.lower() != '.json':
                self.output_file = output_path.parent / f"{output_path.stem}_{timestamp}.json"
            else:
                self.output_file = output_path.parent / f"{output_path.stem}_{timestamp}{output_path.suffix if output_path.suffix else '.txt'}"
        else:
            if self.json_output:
                self.output_file = self.base_dir / f"xss_results_{timestamp}.json"
            else:
                self.output_file = self.base_dir / f"xss_results_{timestamp}.txt"

class XSSScanner:
    async def send_telegram_notification(self, message: str) -> None:
        """Send notification to Telegram if enabled."""
        if not TELEGRAM_NOTIFICATIONS_ENABLED or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return

        try:
            telegram_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            message = (message.replace('<', '&lt;')
                            .replace('>', '&gt;')
                            .replace('&', '&amp;')
                            .replace('"', '&quot;')
                            .replace("'", '&#39;'))
            
            params = {
                'chat_id': TELEGRAM_CHAT_ID,
                'text': message,
                'parse_mode': 'HTML'
            }
            
            async with self.http_session.post(telegram_url, json=params) as response:
                if response.status != 200:
                    error_msg = await response.text()
                    log_error(f"Telegram notification error: {error_msg}")
        except Exception as e:
            log_error(f"Telegram notification error: {str(e)}")

    def _extract_payload_from_url(self, url: str) -> Optional[str]:
        """Extract the payload from a URL by comparing with original parameters"""
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query, keep_blank_values=True)
            # Look for the payload in parameter values
            for param_values in params.values():
                for value in param_values:
                    if any(payload in value for payload in self.payloads):
                        return value
            return None
        except Exception:
            return None

    SVG_PATTERNS = [
        re.compile(r'<\s*svg[^>]*>(.*?)</\s*svg\s*>', re.I | re.S),
        re.compile(r'<\s*svg[^>]*onload\s*=', re.I),
        re.compile(r'<\s*svg[^>]*on\w+\s*=', re.I)
    ]
    JS_PATTERNS = [
        re.compile(r'javascript:.*alert', re.I),
        re.compile(r'javascript:.*confirm', re.I),
        re.compile(r'javascript:.*prompt', re.I),
        re.compile(r'javascript:.*eval', re.I),
        re.compile(r'data:text/html.*base64', re.I)
    ]
    EVENT_PATTERNS = [
        re.compile(r'on\w+\s*=\s*(["\']|&quot;|&#39;)?(\w|\+|\s|\\|&#x|%|!|\^|\(|\)|{|}|\[|\])*?(alert|confirm|prompt|eval)', re.I),
        re.compile(r'on\w+\s*=\s*(["\']|&quot;|&#39;)?(\w|\+|\s|\\|&#x|%|!|\^|\(|\)|{|}|\[|\])*?(location|document|window|this)', re.I)
    ]

    def __init__(self, config: Config) -> None:
        self.config: Config = config
        self.ERROR_LOG_FILE = "logs/errors.log"
        self.stats: Dict[str, int] = {
            'total_urls': 0,
            'parameters_tested': 0,
            'payloads_tested': 0,
            'successful_payloads': 0,
            'failed_payloads': 0,
            'errors': 0
        }
        self.dom_scanner = DOMXSSScanner()
        self.error_types: Dict[str, int] = {}
        self.start_time: Optional[float] = None
        self.output_lock: asyncio.Lock = asyncio.Lock()
        self.stats_lock: asyncio.Lock = asyncio.Lock()
        self.tested_parameters: set = set()
        self.tested_parameters_lock = asyncio.Lock()
        self.results: List[str] = []
        self.json_results: List[Dict[str, Any]] = []
        self.total_tests: int = 0
        self.urls: List[str] = []
        self.payloads: List[str] = []
        self.interrupted: bool = False
        self.running: bool = True
        self.rate_limiter = RateLimiter(config.rate_limit)
        self.progress_bar: Optional[tqdm_asyncio] = None
        self.browser: Optional[Browser] = None
        self.context_pool: asyncio.Queue = asyncio.Queue()
        self.http_session: Optional[aiohttp.ClientSession] = None

    async def initialize_http_session(self) -> None:
        connector = aiohttp.TCPConnector(
            limit=self.config.max_workers * CONNECTIONS_PER_WORKER,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
            force_close=False,
            keepalive_timeout=60,
            ssl=False
        )
        timeout = aiohttp.ClientTimeout(
            total=self.config.timeout,
            connect=self.config.timeout / 2,
            sock_read=self.config.timeout
        )
        self.http_session = aiohttp.ClientSession(
            connector=connector,
            headers=self.config.headers,
            timeout=timeout,
            trust_env=True
        )

    async def close_http_session(self) -> None:
        if self.http_session:
            await self.http_session.close()

    async def initialize_browser(self) -> None:
        try:
            playwright_obj = await async_playwright().start()
            self.browser = await playwright_obj.chromium.launch(headless=True)
            for _ in range(self.config.max_workers):
                context = await self.browser.new_context(
                    extra_http_headers=self.config.headers
                )
                page = await context.new_page()
                await self.context_pool.put((context, page))
        except Exception as e:
            log_error(f"Failed to initialize browser: {str(e)}")
            raise

    async def acquire_context(self) -> Tuple[BrowserContext, Page]:
        return await self.context_pool.get()

    async def release_context(self, context: BrowserContext, page: Page) -> None:
        await self.context_pool.put((context, page))

    def get_parameter_context(self, url: str, param: str) -> str:
        parsed = urlparse(url)
        normalized_path = parsed.path.rstrip('/')
        return f"{parsed.netloc}{normalized_path}:{param.lower()}"

    async def should_test_parameter(self, url: str, param: str) -> bool:
        context = self.get_parameter_context(url, param)
        async with self.tested_parameters_lock:
            if context not in self.tested_parameters:
                self.tested_parameters.add(context)
                return True
            return False

    def inject_payload(self, url: str, payload: str) -> List[Tuple[str, str]]:
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        injected_urls = []
        
        # Only process parameters that have an equals sign in the original URL
        original_params = {param.split('=')[0]: param.split('=')[1] if '=' in param else None 
                          for param in parsed.query.split('&') if param}
        
        for param in params:
            # Skip parameters that didn't have an equals sign in the original URL
            if param not in original_params or original_params[param] is None:
                continue
            
            new_params = params.copy()
            new_params[param] = [payload]
            new_query = urlencode(new_params, doseq=True)
            new_url = urlunparse(parsed._replace(query=new_query))
            injected_urls.append((new_url, param))
        
        return injected_urls

    async def _check_complex_reflection(self, content: str, payload: str) -> bool:
        encoded_variations = self._get_encoded_variations(payload)
        if any(var in content for var in encoded_variations):
            return True
        if 'svg' in payload.lower():
            if any(pattern.search(content) for pattern in self.SVG_PATTERNS):
                return True
        if 'javascript:' in payload.lower():
            if any(pattern.search(content) for pattern in self.JS_PATTERNS):
                return True
        if any(event in payload.lower() for event in ['onload', 'onerror', 'onmouseover']):
            if any(pattern.search(content) for pattern in self.EVENT_PATTERNS):
                return True
        if 'data:' in payload.lower():
            data_patterns = [
                re.compile(r'data:text/html.*,', re.I),
                re.compile(r'data:image/svg.*,', re.I),
                re.compile(r'data:application/x-.*,', re.I)
            ]
            if any(pattern.search(content) for pattern in data_patterns):
                return True
        if 'expression' in payload.lower():
            expr_patterns = [
                re.compile(r'expression\s*\(', re.I),
                re.compile(r'expr\s*\(', re.I)
            ]
            if any(pattern.search(content) for pattern in expr_patterns):
                return True
        if any(char in payload for char in ['`', '+', '${']):
            concat_patterns = [
                re.compile(r'\$\{.*\}', re.S),
                re.compile(r'["\'][\s+]*\+[\s+]*["\']'),
                re.compile(r'`[^`]*\$\{[^}]*\}[^`]*`')
            ]
            if any(pattern.search(content) for pattern in concat_patterns):
                return True
        if 'constructor' in payload:
            constructor_patterns = [
                re.compile(r'constructor\s*\('),
                re.compile(r'constructor\s*\['),
                re.compile(r'\[constructor\]')
            ]
            if any(pattern.search(content) for pattern in constructor_patterns):
                return True
        if re.search(r'(\\x[0-9a-f]{2}|\\u[0-9a-f]{4}|&#x[0-9a-f]+;)', payload, re.I):
            try:
                escaped_payload = re.sub(r'(\\x[0-9a-f]{2}|\\u[0-9a-f]{4}|&#x[0-9a-f]+;)',
                                       lambda m: bytes.fromhex(m.group(1).replace('\\x','')
                                                                       .replace('\\u','')
                                                                       .replace('&#x','')
                                                                       .replace(';','')).decode('utf-8'),
                                       payload,
                                       flags=re.I)
                if escaped_payload in content:
                    return True
            except Exception as e:
                log_error(f"Error decoding escape sequences: {str(e)}")
                return False
        return False

    def _get_encoded_variations(self, payload: str) -> List[str]:
        variations = []
        variations.append(urllib.parse.quote(payload))
        variations.append(urllib.parse.quote_plus(payload))
        variations.append(html.escape(payload))
        variations.append(html.escape(payload, quote=False))
        variations.append(urllib.parse.quote(urllib.parse.quote(payload)))
        variations.append(html.escape(html.escape(payload)))
        variations.append(payload.encode('unicode-escape').decode())
        variations.append(base64.b64encode(payload.encode()).decode())
        variations.extend([
            ''.join([f'&#{ord(c)};' for c in payload]),
            ''.join([f'&#x{ord(c):x};' for c in payload])
        ])
        return variations

    async def validate_alert(self, page: Page, url: str, potential_dom: bool = False) -> Tuple[bool, Optional[str], str]:
        """Validate both reflected and DOM XSS with a single browser instance"""
        alert_triggered = False
        alert_text = None
        xss_type = 'unknown'
        dom_detected = False
        is_reflected = False
        payload = self._extract_payload_from_url(url)

        async def handle_dialog(dialog):
            nonlocal alert_triggered, alert_text
            alert_text = dialog.message
            await dialog.accept()
            alert_triggered = True

        try:
            page.on("dialog", handle_dialog)
            
            # First get and analyze the raw response without browser rendering
            async with self.http_session.get(url) as response:
                content = await response.text()
                
                # Check for reflection in raw response
                if payload:
                    is_reflected = payload in content or payload in html.unescape(content)
                    for encoded_var in self._get_encoded_variations(payload):
                        if encoded_var in content:
                            is_reflected = True
                            break
                
                # Look for DOM XSS patterns in source
                dom_patterns = [
                    (r'document\.getElementById.*\.innerHTML', ''),
                    (r'location\.(?:search|hash|href).*(?:innerHTML|eval|document\.write)', ''),
                    (r'URLSearchParams.*innerHTML', ''),
                    (r'window\.onload.*innerHTML', ''),
                    (r'document\.write\s*\(.*\)', ''),
                    (r'eval\s*\(.*\)', ''),
                    (r'setTimeout\s*\(.*\)', ''),
                    (r'setInterval\s*\(.*\)', ''),
                    (r'new\s+Function\s*\(.*\)', '')
                ]
                
                for pattern, _ in dom_patterns:
                    if re.search(pattern, content, re.IGNORECASE | re.DOTALL):
                        dom_detected = True
                        break  # Found one pattern, no need to check others

                # If found DOM patterns, add monitoring
                if dom_detected:
                    await page.add_init_script("""
                        window.addEventListener('DOMContentLoaded', () => {
                            // Monitor DOM modifications
                            const observer = new MutationObserver((mutations) => {
                                mutations.forEach((mutation) => {
                                    if (mutation.type === 'childList' || mutation.type === 'characterData') {
                                        window._domModified = true;
                                    }
                                });
                            });
                            observer.observe(document.body, {
                                childList: true,
                                characterData: true,
                                subtree: true
                            });
                            
                            // Monitor script execution
                            const originalSetTimeout = window.setTimeout;
                            window.setTimeout = function() {
                                window._scriptExecuted = true;
                                return originalSetTimeout.apply(this, arguments);
                            };
                            
                            const originalEval = window.eval;
                            window.eval = function() {
                                window._scriptExecuted = true;
                                return originalEval.apply(this, arguments);
                            };
                        });
                    """)

                # Navigate and wait for load
                await page.goto(url, timeout=self.config.timeout * 1000)
                await page.wait_for_load_state('domcontentloaded')
                
                # Wait for potential JavaScript execution
                await page.wait_for_timeout(self.config.alert_timeout * 1000)
                
                # Check for DOM modifications silently
                if dom_detected:
                    dom_modified = await page.evaluate("() => window._domModified === true")
                    script_executed = await page.evaluate("() => window._scriptExecuted === true")
                    if dom_modified or script_executed:
                        dom_detected = True

                # Determine XSS type based on our findings
                if alert_triggered:
                    if is_reflected and dom_detected:
                        xss_type = 'both'
                    elif is_reflected:
                        xss_type = 'reflected'
                    elif dom_detected:
                        xss_type = 'dom'
                    else:
                        # If got an alert but couldn't determine type, default to reflected
                        xss_type = 'reflected'

                return alert_triggered, alert_text, xss_type

        except Exception as e:
            async with self.stats_lock:
                self.stats['errors'] += 1
                self.error_types[type(e).__name__] = self.error_types.get(type(e).__name__, 0) + 1
            log_error(f"Error validating alert for {url}: {str(e)}")
            return False, None, 'reflected'
        finally:
            page.remove_listener("dialog", handle_dialog)

    async def record_vulnerability(self, domain: str, param: str, payload: str,
                                   url: str, alert_text: str, xss_type: str = 'reflected') -> None:
        async with self.output_lock:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            result = {
                "timestamp": timestamp,
                "domain": domain,
                "parameter": param,
                "payload": payload,
                "url": url,
                "alert_text": alert_text,
                "type": xss_type
            }
            
            # Prepare and send Telegram notification
            notification_message = (
                f"🎯 XSS Vulnerability Found!\n\n"
                f"🔍 Type: {xss_type.capitalize()} XSS\n"
                f"🌐 Domain: {domain}\n"
                f"📝 Parameter: {param}\n"
                f"💉 Payload: {payload}\n"
                f"🔗 URL: {url}\n"
                f"⚠️ Alert Text: {alert_text}\n"
                f"🕒 Time: {timestamp}"
            )
            await self.send_telegram_notification(notification_message)
            
            if self.config.json_output:
                self.json_results.append(result)
            else:
                self.results.extend([
                    "XSS Found: ",
                    f"Type: {xss_type.capitalize()} XSS",
                    f"Domain: {domain}",
                    f"Parameter: {param}",
                    f"Payload: {payload}",
                    f"URL: {url}",
                    f"Alert Text: {alert_text}",
                    ""
                ])

    async def process_parameter_payloads(self, url: str, param: str, progress_bar: tqdm_asyncio) -> None:
        domain = urlparse(url).netloc
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        
        try:
            for payload in self.payloads:
                if not self.running:
                    break
                    
                # Apply rate limiting
                await self.rate_limiter.acquire()
                
                new_params = params.copy()
                new_params[param] = [payload]
                new_query = urlencode(new_params, doseq=True)
                test_url = urlunparse(parsed._replace(query=new_query))
                
                async with self.stats_lock:
                    self.stats['payloads_tested'] += 1
                
                try:
                    # Get initial response
                    async with self.http_session.get(test_url) as response:
                        if response.status != 200:
                            # Update progress even for failed requests
                            if progress_bar:
                                progress_bar.update(1)
                            continue
                            
                        response_text = await response.text()
                        is_reflected = payload in response_text
                        potential_dom = await self.dom_scanner.quick_dom_check(response_text)

                        # Skip Playwright check if no reflection and no DOM potential
                        if not is_reflected and not potential_dom:
                            async with self.stats_lock:
                                self.stats['failed_payloads'] += 1
                            if progress_bar:
                                progress_bar.update(1)
                            continue

                        # Single Playwright check that handles both types
                        context, page = await self.acquire_context()
                        try:
                            alert_triggered, alert_text, xss_type = await self.validate_alert(
                                page, 
                                test_url, 
                                potential_dom=potential_dom
                            )

                            if alert_triggered:
                                async with self.stats_lock:
                                    self.stats['successful_payloads'] += 1
                                
                                payload_number = self.payload_index_map.get(payload, 0)
                                type_label = "DOM Based" if xss_type == "dom" else "Reflected"

                                # Format results fields with fixed widths for alignment
                                domain_field = f"{domain:<25}"  # Pad domain to 25 chars
                                param_field = f"{param:<10}"    # Pad parameter to 10 chars
                                type_field = f"{type_label:<10}" # Pad type to 12 chars
                                
                                message = (
                                    f"{Fore.GREEN}🎯 XSS Found!{Style.RESET_ALL}  "
                                    f"Domain: {Fore.YELLOW}{domain_field}{Style.RESET_ALL}  |  "
                                    f"Parameter: {Fore.YELLOW}{param_field}{Style.RESET_ALL}  |  "
                                    f"Type: {Fore.YELLOW}{type_field}{Style.RESET_ALL}  |  "
                                    f"Payload: {Fore.YELLOW}#{payload_number}{Style.RESET_ALL}"
                                )
                                
                                if progress_bar:
                                    progress_bar.write(message)
                                
                                await self.record_vulnerability(
                                    domain=domain,
                                    param=param,
                                    payload=payload,
                                    url=test_url,
                                    alert_text=alert_text,
                                    xss_type=type_label
                                )
                                
                                # Update progress and skip remaining payloads for this parameter
                                remaining_payloads = len(self.payloads) - (self.payloads.index(payload) + 1)
                                if progress_bar:
                                    # Update for current test and remaining payloads that will be skipped
                                    progress_bar.update(1 + remaining_payloads)
                                break
                            else:
                                async with self.stats_lock:
                                    self.stats['failed_payloads'] += 1
                                if progress_bar:
                                    progress_bar.update(1)
                                    
                        finally:
                            await self.release_context(context, page)

                except Exception as e:
                    async with self.stats_lock:
                        self.stats['errors'] += 1
                        self.error_types[type(e).__name__] = self.error_types.get(type(e).__name__, 0) + 1
                    # Update progress even when errors occur
                    if progress_bar:
                        progress_bar.update(1)
                    # Write error to log file instead of printing
                    with open(self.ERROR_LOG_FILE, 'a', encoding='utf-8') as f:
                        f.write(f"Error processing {test_url}: {str(e)}\n")
                
        except Exception as e:
            log_error(f"Error in parameter payload processing: {str(e)}")
            # Update progress for any remaining tests in case of errors
            if progress_bar:
                remaining = len(self.payloads)
                progress_bar.update(remaining)

    async def process_batch(self, batch_urls: List[str], progress_bar: tqdm_asyncio) -> None:
        try:
            # Use a smaller number of concurrent workers for better control
            semaphore = asyncio.Semaphore(MAX_WORKERS_PER_BATCH)
            tasks = []
            
            async def process_with_semaphore(url: str, param: str) -> None:
                try:
                    async with semaphore:
                        await self.process_parameter_payloads(url, param, progress_bar)
                        # Small delay after each parameter test
                        await asyncio.sleep(0.1)
                except Exception as e:
                    log_error(f"Error processing URL {url} with parameter {param}: {str(e)}")
                    if progress_bar:
                        progress_bar.update(1)
            
            # Process URLs in sequential batches
            for url in batch_urls:
                if not self.running:
                    break
                
                try:
                    parsed = urlparse(url)
                    params = parse_qs(parsed.query, keep_blank_values=True)
                    
                    # Only create tasks for valid parameters
                    for param in params:
                        if await self.should_test_parameter(url, param):
                            task = asyncio.create_task(process_with_semaphore(url, param))
                            tasks.append(task)
                    
                    # Process tasks in smaller chunks for better control
                    while tasks:
                        # Take up to x tasks at a time
                        current_tasks, tasks = tasks[:MAX_WORKERS_PER_BATCH], tasks[MAX_WORKERS_PER_BATCH:]
                        if current_tasks:
                            await asyncio.gather(*current_tasks, return_exceptions=True)
                        # Add a small delay between chunks
                        await asyncio.sleep(0.2)
                    
                except Exception as e:
                    log_error(f"Error processing batch URL {url}: {str(e)}")
                    continue

        except Exception as e:
            log_error(f"Error in process_batch: {str(e)}")

    def banner(self) -> None:
        banner_width = 91
        def center_text(text: str, width: int = banner_width) -> str:
            return text.center(width)
        banner = f"""
{Fore.CYAN}██╗  ██╗███████╗███████╗██║   ██║ ██████╗ ██████╗███████╗███████╗███████╗ ██████╗ ██████╗ 
╚██╗██╔╝██╔════╝██╔════╝██║   ██║██╔════╝██╔════╝██╔════╝██╔════╝██╔════╝██╔═══██╗██╔══██╗
 ╚███╔╝ ███████╗███████╗██║   ██║██║     ██║     █████╗  ███████╗███████╗██║   ██║██████╔╝
 ██╔██╗ ╚════██║╚════██║██║   ██║██║     ██║     ██╔══╝  ╚════██║╚════██║██║   ██║██╔══██╗
██╔╝ ██╗███████║███████║╚██████╔╝╚██████╗╚██████╗███████╗███████║███████║╚██████╔╝██║  ██║
╚═╝  ╚═╝╚══════╝╚══════╝ ╚═════╝  ╚═════╝╚═════╝╚══════╝╚══════╝╚══════╝ ╚═════╝ ╚═╝  ╚═╝
{center_text(f"{Fore.CYAN}By Dimitris Chatzidimitris{Style.RESET_ALL}")}
{center_text(f"{Fore.CYAN}Email: dimitris.chatzidimitris@gmail.com{Style.RESET_ALL}")}
{center_text(f"{Fore.CYAN}Async-Powered / 100% Valid Results / Bypasses Cloud/WAF{Style.RESET_ALL}")}\n
{Fore.CYAN}🔧Configuration:{Style.RESET_ALL}
- Version: {Fore.GREEN}{self.config.current_version}{Style.RESET_ALL}
- Update Available: {Fore.GREEN}{self.config.version_info.update_available}{Style.RESET_ALL}
- Max Workers: {Fore.GREEN}{self.config.max_workers}{Style.RESET_ALL}
- Payloads Batch Size: {Fore.GREEN}{self.config.batch_size}{Style.RESET_ALL}
- Rate Limit: {Fore.GREEN}{self.config.rate_limit} req/s{Style.RESET_ALL}
- Page Timeout: {Fore.GREEN}{self.config.timeout}s{Style.RESET_ALL}
- Alert Timeout: {Fore.GREEN}{self.config.alert_timeout}s{Style.RESET_ALL}
- Playwright Version: {Fore.GREEN}{self.config.playwright_version}{Style.RESET_ALL}
- Payloads File: {Fore.GREEN}{self.config.payload_file}{Style.RESET_ALL}
- Custom Headers: {Fore.GREEN}{'Yes' if self.config.custom_headers_present else 'Default'}{Style.RESET_ALL}
- Output Format: {Fore.GREEN}{'JSON' if self.config.json_output else 'Text'}{Style.RESET_ALL}
- Output File: {Fore.GREEN}{str(self.config.output_file.resolve())}{Style.RESET_ALL}\n"""
        print(banner)

    async def check_reflection(self, url: str, payload: str) -> bool:
        try:
            async with self.http_session.get(url) as response:
                if response.status != 200:
                    return False
                content = await response.text()
                if payload in content:
                    return True
                if await self._check_complex_reflection(content, payload):
                    return True
                return False
        except Exception as e:
            async with self.stats_lock:
                self.stats['errors'] += 1
                self.error_types[type(e).__name__] = self.error_types.get(type(e).__name__, 0) + 1
            return False

    async def run(self) -> None:
        self.banner()
        await self.load_files()
        self.start_time = time.time()
        
        try:
            await self.initialize_http_session()
            await self.initialize_browser()
            
            # Warm up only a few initial connections
            async def warm_up_connection(url: str) -> None:
                try:
                    async with self.http_session.head(url, timeout=2):
                        pass
                except Exception:
                    pass
            
            # Warm up with just first 3 URLs instead of 10
            first_batch = self.urls[:min(3, len(self.urls))]
            await asyncio.gather(*[warm_up_connection(url) for url in first_batch])
            
            # Initialize progress bar with more descriptive format
            self.progress_bar = tqdm_asyncio(
                total=self.total_tests,
                desc="Progress",
                bar_format=(
                    "Progress: {percentage:3.0f}%|{bar}| "
                    f"[URLs: {{n_fmt}}/{len(self.urls)}] "
                    "[{n_fmt}/{total_fmt} Tests] "
                    "[Time:{elapsed} - Est:{remaining}]"
                ),
                colour="cyan",
                dynamic_ncols=True,
                unit="test"
            )
            
            # Process URLs in smaller batches with better progress tracking
            total_urls = len(self.urls)
            
            for i in range(0, total_urls, URLS_BATCH_SIZE):
                batch_urls = self.urls[i:i + URLS_BATCH_SIZE]
                current_batch_end = min(i + URLS_BATCH_SIZE, total_urls)
                if not self.running:
                    break
                
                # Update progress description to show current batch
                self.progress_bar.bar_format = (
                    "Progress: {percentage:3.0f}%|{bar}| "
                    f"[URLs: {i+1}-{current_batch_end}/{len(self.urls)}] "
                    "[{n_fmt}/{total_fmt} Tests] "
                    "[Time:{elapsed} - Est:{remaining}]"
                )
                
                # Process the current batch
                try:
                    await self.process_batch(batch_urls, self.progress_bar)
                except Exception as e:
                    log_error(f"Error processing batch {i+1}-{current_batch_end}: {str(e)}")
                    continue
                
                # Add delay between batches
                await asyncio.sleep(0.5)
            
            await asyncio.sleep(0)
        except Exception as e:
            log_error(f"Error in run method: {str(e)}")
        finally:
            await self.cleanup()

    async def cleanup(self) -> None:
        try:
            # Close progress bar first
            if self.progress_bar:
                try:
                    remaining = self.progress_bar.total - self.progress_bar.n
                    if remaining > 0:
                        self.progress_bar.update(remaining)
                    self.progress_bar.refresh()
                    self.progress_bar.close()
                except Exception:
                    pass
                self.progress_bar = None

            # Reset rate limiter if exists
            if hasattr(self, 'rate_limiter'):
                self.rate_limiter.reset()

            # Save results if we found vulnerabilities
            if self.stats['successful_payloads'] > 0:
                await self.save_results()

            # Clean up browser contexts
            if hasattr(self, 'context_pool') and self.context_pool is not None:
                while not self.context_pool.empty():
                    try:
                        context, page = await self.context_pool.get_nowait()
                        if page:
                            await page.close()
                        if context:
                            await context.close()
                    except Exception:
                        pass

            # Close browser with timeout
            if hasattr(self, 'browser') and self.browser:
                try:
                    await asyncio.wait_for(self.browser.close(), timeout=2.0)
                except Exception:
                    pass

            # Close HTTP session with timeout
            if hasattr(self, 'http_session') and self.http_session and not self.http_session.closed:
                try:
                    await asyncio.wait_for(self.http_session.close(), timeout=1.0)
                    await asyncio.sleep(0.1)  # Small delay to ensure proper closure
                except Exception:
                    pass

            # Print final stats if not interrupted
            if not self.interrupted:
                self.print_final_stats()

        except Exception as e:
            # Log any cleanup errors but don't raise them
            log_error(f"Error during cleanup: {str(e)}")
            
        finally:
            # Nullify resources in a specific order
            if hasattr(self, 'context_pool'):
                self.context_pool = None
            if hasattr(self, 'browser'):
                self.browser = None
            if hasattr(self, 'http_session'):
                self.http_session = None
            
            # Suppress ResourceWarning for unclosed client session
            warnings.filterwarnings("ignore",
                                  category=ResourceWarning,
                                  message="unclosed.*<aiohttp.client.ClientSession.*>")

    async def save_results(self) -> None:
        try:
            if self.config.json_output:
                async with aiofiles.open(self.config.output_file, 'w') as f:
                    await f.write(json.dumps(self.json_results, indent=2))
            else:
                async with aiofiles.open(self.config.output_file, 'w') as f:
                    await f.write('\n'.join(self.results))
        except Exception as e:
            print(f"\n{Fore.RED}Error saving results: {str(e)}{Style.RESET_ALL}")
            log_error(f"Error saving results: {str(e)}")

    def print_final_stats(self) -> None:
        if self.start_time:
            duration = time.time() - self.start_time
            minutes, seconds = divmod(int(duration), 60)
            print(f"\n{Fore.CYAN}🏁 Scan Complete! Summary:{Style.RESET_ALL}")
            print("="*30)
            print(f"Duration: {Fore.GREEN}{minutes}m {seconds}s{Style.RESET_ALL}")
            print(f"URLs tested: {Fore.GREEN}{self.stats['total_urls']}{Style.RESET_ALL}")
            print(f"Parameters tested: {Fore.GREEN}{self.stats['parameters_tested']}{Style.RESET_ALL}")
            print(f"Payloads tested: {Fore.GREEN}{self.stats['payloads_tested']}{Style.RESET_ALL}")
            print(f"Successful payloads: {Fore.GREEN}{self.stats['successful_payloads']}{Style.RESET_ALL}")
            print(f"Failed/skipped payloads: {Fore.YELLOW}{self.stats['failed_payloads']}{Style.RESET_ALL}")
            print(f"Errors encountered: {Fore.RED if self.stats['errors'] > 0 else Fore.GREEN}{self.stats['errors']}{Style.RESET_ALL}")
            if self.stats['errors'] > 0 and self.error_types:
                for err_type, count in self.error_types.items():
                    pass
            print("="*30)
            if self.stats['successful_payloads'] > 0:
                print(f"\n{Fore.CYAN}📝 Results saved to:{Style.RESET_ALL} {Fore.GREEN}{self.config.output_file.resolve()}{Style.RESET_ALL}")

    def stop(self) -> None:
        self.running = False
        self.interrupted = True

    def get_url_signature(self, url: str) -> str:
        """
        Creates a signature for a URL based only on parameter names and positions.
        Values are stripped out but order is preserved.
        """
        try:
            parsed = urlparse(url)
            
            # Handle parameters in fragment (for SPAs)
            if '#' in url and '?' in url.split('#')[1]:
                fragment_parts = url.split('#')[1].split('?')
                query = fragment_parts[1]
            else:
                query = parsed.query
                
            # Get parameters in order, remove values
            params = []
            if query:
                for param in query.split('&'):
                    if '=' in param:
                        name = param.split('=')[0]
                        if name:
                            params.append(f"{name}=")
                            
            # Reconstruct URL with stripped values
            base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            if params:
                return f"{base}?{'&'.join(params)}"
            return base
        except Exception as e:
            logging.error(f"Error generating URL signature: {str(e)}")
            return url

    async def load_files(self) -> None:
        try:
            print(f"{Fore.YELLOW}📦 Loading URLs...{Style.RESET_ALL}")
            
            if self.config.url_list:
                async with aiofiles.open(self.config.url_list, 'r') as f:
                    all_urls = [line.strip() for line in await f.readlines() if line.strip()]
                    valid_urls = []
                    seen_signatures = set()
                    skipped_same_params = 0
                    
                    for url in all_urls:
                        # First check if it's a valid URL with parameters
                        if not validate_url(url):
                            continue
                            
                        # Get signature (strips values but keeps param order)
                        signature = self.get_url_signature(url)
                        
                        # Only add if we haven't seen this parameter combination
                        if signature not in seen_signatures:
                            seen_signatures.add(signature)
                            valid_urls.append(url)
                        else:
                            skipped_same_params += 1
                    
                    if not valid_urls:
                        print(f"\n{Fore.RED}❌ Error: No valid URLs found in the input file.{Style.RESET_ALL}")
                        print(f"{Fore.YELLOW}🧩 URLs must include parameters. Examples:{Style.RESET_ALL}")
                        print(f"{Fore.CYAN}- http://testhtml5.vulnweb.com/#/redir?url={Style.RESET_ALL}")
                        print(f"{Fore.CYAN}- http://testhtml5.vulnweb.com/%23/redir?url=value{Style.RESET_ALL}")
                        sys.exit(2)
                    
                    # Print results
                    print(f"{Fore.CYAN}📝 URL Processing Results:{Style.RESET_ALL}")
                    print(f"{Fore.GREEN}✓ Total URLs: {len(all_urls)}{Style.RESET_ALL}")
                    print(f"{Fore.GREEN}✓ Unique parameter combinations: {len(valid_urls)}{Style.RESET_ALL}")
                    if skipped_same_params > 0:
                        print(f"{Fore.GREEN}ℹ Skipped URLs (same parameters): {skipped_same_params}{Style.RESET_ALL}")
                    
                    self.urls = valid_urls
                    
            elif self.config.domain:
                # Single domain validation already done in parse_arguments
                self.urls = [self.config.domain]
                
            async with aiofiles.open(self.config.payload_file, 'r') as f:
                self.payloads = list(dict.fromkeys([line.strip() for line in await f.readlines() if line.strip()]))
                self.payload_index_map = {payload: idx + 1 for idx, payload in enumerate(self.payloads)}
            
            self.stats['total_urls'] = len(self.urls)
            total_parameters = 0
            for url in self.urls:
                decoded_url = urllib.parse.unquote(url)
                # Get query part from either normal query or after fragment
                query_part = ""
                if '#' in decoded_url and '?' in decoded_url.split('#')[1]:
                    # Parameters are after the fragment
                    fragment_parts = decoded_url.split('#')[1]
                    query_part = fragment_parts.split('?')[1]
                elif '?' in decoded_url:
                    # Normal query parameters
                    query_part = decoded_url.split('?')[1]
                
                # Count parameters that have an equals sign
                params = []
                for param in query_part.split('&'):
                    if param and '=' in param:
                        param_name = param.split('=')[0]
                        if param_name:  # Ensure parameter name exists
                            params.append(param_name)
                
                total_parameters += len(params)
            
            self.stats['parameters_tested'] = total_parameters
            self.total_tests = total_parameters * len(self.payloads)
            
            print(f"{Fore.CYAN}🔗 Loaded {len(self.urls)} URLs and {len(self.payloads)} payloads{Style.RESET_ALL}")
            print(f"{Fore.YELLOW}🔍 Starting the scan...{Style.RESET_ALL}\n")
            
        except FileNotFoundError as e:
            print(f"{Fore.RED}📂 Error: Could not find file: {e.filename}{Style.RESET_ALL}")
            sys.exit(1)
        except Exception as e:
            print(f"{Fore.RED}❌ An unexpected error occurred while loading files: {str(e)}{Style.RESET_ALL}")
            sys.exit(1)

async def async_main() -> None:
    scanner = None
    try:
        playwright_version = check_playwright_version()
        args = parse_arguments()
        if args.update:
            git_handler = GitHandler()
            if not git_handler.ensure_git_available():
                print(f"\n{Fore.RED}Cannot perform update without Git installed.{Style.RESET_ALL}")
                return
            print(f"\n{Fore.CYAN}Checking for updates...{Style.RESET_ALL}")
            updater = AutoUpdater()
            update_result = updater.check_and_update()
            if update_result.get('status') == 'error':
                print(f"{Fore.RED}Update check failed: {update_result.get('message')}{Style.RESET_ALL}")
                return
            elif update_result.get('updated'):
                print(f"{Fore.GREEN}Tool updated successfully to the latest version!{Style.RESET_ALL}")
                if update_result.get('changes'):
                    print(f"\nUpdated files:\n{update_result.get('changes')}")
                print(f"{Fore.YELLOW}Please restart the tool to use the new version.{Style.RESET_ALL}")
                return
            elif update_result.get('message') == 'Check skipped':
                print(f"{Fore.YELLOW}Check skipped{Style.RESET_ALL}")
                return
            else:
                print(f"{Fore.GREEN}Already running the latest version.{Style.RESET_ALL}")
                return
        config = Config(args, playwright_version)
        setup_logging(config)
        scanner = XSSScanner(config)
        await scanner.run()
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}🚫 Scan interrupted by user{Style.RESET_ALL}")
    except Exception as e:
        print(f"\n{Fore.RED}Error: {str(e)}{Style.RESET_ALL}")
        logging.error(f"Unhandled exception: {str(e)}", exc_info=True)
    finally:
        pass

def setup_logging(config: Config) -> None:
    """Configure logging with separated concerns for different log files"""
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    
    # Clear any existing handlers
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Create formatters
    detailed_formatter = logging.Formatter('%(asctime)s %(levelname)s:%(message)s')
    console_formatter = logging.Formatter('%(message)s')

    # Main log file handler - captures DEBUG through INFO
    file_handler = logging.FileHandler(logs_dir / 'xss_scanner.log')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(detailed_formatter)
    file_handler.addFilter(lambda record: record.levelno < logging.ERROR)  # Only non-error messages

    # Error log file handler - captures ERROR and CRITICAL
    error_handler = logging.FileHandler(logs_dir / 'errors.log')
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(detailed_formatter)

    # Console handler - for immediate feedback
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.ERROR)
    console_handler.setFormatter(console_formatter)
    console_handler.addFilter(TimeoutFilter())

    # Add all handlers to root logger
    root_logger.addHandler(file_handler)
    root_logger.addHandler(error_handler)
    root_logger.addHandler(console_handler)

class HeaderManager:
    @staticmethod
    def get_default_headers() -> Dict[str, str]:
        chrome_versions = ["122.0.6261.112", "122.0.6261.94", "122.0.6261.69"]
        viewport_widths = [1366, 1440, 1536, 1920, 2560]
        device_memories = [2, 4, 8, 16]
        languages = [
            'en-US,en;q=0.9',
            'en-US,en;q=0.9,es;q=0.8',
            'en-GB,en;q=0.9,en-US;q=0.8',
            'en-US,en;q=0.9,fr;q=0.8'
        ]
        chrome_version = random.choice(chrome_versions)
        viewport = random.choice(viewport_widths)
        memory = random.choice(device_memories)
        language = random.choice(languages)
        base_headers = {
            'User-Agent': f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version} Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': language,
            'Accept-Encoding': 'gzip, deflate, br',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
            'Sec-Ch-Ua': f'"Chromium";v="{chrome_version}", "Google Chrome";v="{chrome_version}", "Not(A:Brand";v="24"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Sec-Ch-Ua-Platform-Version': '"15.0.0"',
            'Sec-Ch-Ua-Full-Version-List': f'"Chromium";v="{chrome_version}", "Google Chrome";v="{chrome_version}", "Not(A:Brand";v="24.0.0.0"',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Upgrade-Insecure-Requests': '1',
            'Connection': 'keep-alive',
            'DNT': '1',
            'Viewport-Width': str(viewport),
            'Device-Memory': f'{memory}',
            'Priority': 'u=0, i',
            'Permissions-Policy': 'interest-cohort=()',
        }
        return base_headers

    @staticmethod
    def merge_headers(default_headers: Dict[str, str], custom_headers: Dict[str, str] = None) -> Dict[str, str]:
        if not custom_headers:
            return default_headers
        merged = default_headers.copy()
        custom_headers = {k.title(): v for k, v in custom_headers.items()}
        merged.update(custom_headers)
        return merged

    @staticmethod
    def get_headers(custom_headers: Dict[str, str] = None) -> Dict[str, str]:
        default_headers = HeaderManager.get_default_headers()
        return HeaderManager.merge_headers(default_headers, custom_headers)

class RateLimiter:
    def __init__(self, rate_limit: int):
        self.rate_limit = rate_limit
        self.tokens = rate_limit
        self.last_update = time.time()
        self.lock = asyncio.Lock()
        self._sleep_time = 1.0 / rate_limit if rate_limit > 0 else 0
    
    async def acquire(self):
        """Wait until a token is available for making a request."""
        async with self.lock:
            now = time.time()
            time_passed = now - self.last_update
            self.tokens = min(self.rate_limit, self.tokens + time_passed * self.rate_limit)
            self.last_update = now
            
            if self.tokens < 1:
                sleep_time = self._sleep_time
                await asyncio.sleep(sleep_time)
                self.tokens = 1
            
            self.tokens -= 1
    
    def reset(self):
        """Reset the rate limiter state."""
        self.tokens = self.rate_limit
        self.last_update = time.time()

def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}🚫 Scan interrupted by user{Style.RESET_ALL}")
        sys.exit(0)
    except Exception as e:
        print(f"\n{Fore.RED}Error: {str(e)}{Style.RESET_ALL}")
        logging.error(f"Unhandled exception: {str(e)}", exc_info=True)
        sys.exit(1)
    finally:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.stop()
            if not loop.is_closed():
                loop.close()
        except:
            pass
        cleanup_stderr()

if __name__ == "__main__":
    main()
