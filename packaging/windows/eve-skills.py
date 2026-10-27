"""Entry script for the frozen Windows build (see eve-skills.spec).

`eve_skills/__main__.py` uses a relative import, which PyInstaller cannot run as a top-level script,
so the executable starts here. It also covers the one thing a console program does badly on Windows:
someone double-clicks eve-skills.exe in Explorer. Windows opens a console just for it, the default
`skills` command fails for want of a login, and the window closes before anyone could read why. That
case gets a short explanation and waits for Enter; every run from a terminal behaves exactly like
`python -m eve_skills`.
"""

import sys

from eve_skills.cli import main


def _launched_from_explorer() -> bool:
    """True when this process owns a console nobody else is attached to.

    A onefile build is two processes - the bootloader and the Python child - and both are attached
    to the console. From PowerShell or cmd the shell is attached as well, so a count of two or fewer
    means Windows created the console for us: a double-click."""
    try:
        import ctypes
        processes = (ctypes.c_uint32 * 8)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(processes, len(processes))
    except (AttributeError, OSError):
        return False                       # not Windows, or no console at all
    return 0 < count <= 2


def _explain_and_wait() -> None:
    print("eve-skills is a command-line tool: it runs inside a terminal rather than in its own window.\n"
          "\n"
          "Open PowerShell or Windows Terminal, go to the folder that holds eve-skills.exe, and run\n"
          "    .\\eve-skills.exe --help\n"
          "(or just `eve-skills --help` once that folder is on your PATH; see README.txt next to it).\n")
    try:
        input("Press Enter to close this window.")
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    if len(sys.argv) == 1 and _launched_from_explorer():
        _explain_and_wait()
        sys.exit(0)
    sys.exit(main())
