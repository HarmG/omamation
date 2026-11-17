"""Runs external commands for the blocks; tests replace it with a fake."""

import asyncio
from dataclasses import dataclass


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str


class CommandMissing(Exception):
    """A tool Omamation needs is not installed."""


class System:
    async def run(self, argv: list[str], timeout: float | None = None) -> Result:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise CommandMissing(f"{argv[0]} is not installed") from exc
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # Kill the process when the call times out or is cancelled.
            proc.kill()
            await proc.wait()
            raise
        return Result(proc.returncode, out.decode(errors="replace"), err.decode(errors="replace"))

    async def stream(self, argv: list[str]):
        """Start a long-running command and yield its output lines."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise CommandMissing(f"{argv[0]} is not installed") from exc
        try:
            while line := await proc.stdout.readline():
                yield line.decode(errors="replace")
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
