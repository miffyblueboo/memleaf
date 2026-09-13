"""Windows process-creation flags for child processes that must stay invisible.

memleaf starts three kinds of child process: the detached extraction worker,
host CLI probes, and host model-discovery commands.  None of them owns a
window, and all of them can be started by a host that has no console of its
own -- a Hermes desktop process, for example.  On Windows a console program
started without an explicit flag in that situation gets a brand-new console
window, which lands on the user's desktop as a stray terminal.

``DETACHED_PROCESS`` looks like the right answer and is not: a console program
still needs a console, and Windows creates a visible one for it.  Measured on
Windows 11 with Windows Terminal as the default host, spawning this interpreter
with ``CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`` opened one new console
window whose title was the interpreter path, while ``CREATE_NO_WINDOW`` opened
none.  ``CREATE_NO_WINDOW`` is therefore used, combined with
``CREATE_NEW_PROCESS_GROUP`` so the child keeps its own group and is not taken
down by a Ctrl+C delivered to the parent's console.
"""

from __future__ import annotations

import os
import subprocess


def hidden_process_flags() -> int:
    """Return creation flags that keep a console child windowless on Windows."""

    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )


def hidden_popen_kwargs() -> dict[str, int]:
    """Return ``Popen``/``run`` keyword arguments for a windowless child."""

    flags = hidden_process_flags()
    return {"creationflags": flags} if flags else {}


__all__ = ["hidden_popen_kwargs", "hidden_process_flags"]
