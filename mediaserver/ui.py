"""Terminal output in setup's style: headings, ✓ ok, ! warnings, ✗ errors."""
from __future__ import annotations

import sys


class SetupError(SystemExit):
    """A problem that stops the operation (printed as ✗, exit status 1)"""

    def __init__(self, message: str):
        super().__init__(1)
        self.message = message


def info(message: str) -> None:
    print(f"\n\033[1;34m=> {message}\033[0m", flush=True)


def ok(message: str) -> None:
    print(f"\033[1;32m   ✓ {message}\033[0m", flush=True)


def warn(message: str) -> None:
    print(f"\033[1;33m   ! {message}\033[0m", flush=True)


def err(message: str) -> SetupError:
    """Print the problem and return the exception to raise: raise err(...)"""
    print(f"\033[1;31m   ✗ {message}\033[0m", file=sys.stderr, flush=True)
    return SetupError(message)


def mask(secret: str) -> str:
    """Only the start of a secret, for output"""
    return secret[:6] + "…"
