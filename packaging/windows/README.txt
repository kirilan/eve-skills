eve-skills for Windows
======================

A command line tool for EVE Online characters: skills, training queue, market prices, industry
jobs and your own market orders. This is the standalone build: eve-skills.exe carries its own
Python, so nothing else needs to be installed.

Full documentation: https://github.com/kirilan/eve-skills#readme


1. Put it somewhere permanent
-----------------------------

Extract this zip into a folder that will stay, for example

    %LOCALAPPDATA%\Programs\eve-skills

To be able to type just `eve-skills` in any terminal, add that folder to your user PATH once.
In PowerShell:

    $dir = "$env:LOCALAPPDATA\Programs\eve-skills"
    $path = [Environment]::GetEnvironmentVariable("Path", "User")
    [Environment]::SetEnvironmentVariable("Path", "$path;$dir", "User")

then close and reopen the terminal. Without that step, run it as .\eve-skills.exe from inside
the folder.


2. Run it from a terminal
-------------------------

eve-skills is a command line program: double-clicking it only shows a short note. Open
PowerShell or Windows Terminal and try:

    eve-skills --version
    eve-skills market Tritanium --hub jita     (public prices, no login needed)
    eve-skills --help


3. Log in a character
---------------------

    eve-skills update-data                     (game data, ~100 MB, once per game patch)
    eve-skills login --client-id <your-client-id> --scopes all

Getting a client id (a free EVE developer application) is explained in the README's quick start:
https://github.com/kirilan/eve-skills#quick-start


Where it keeps its files
------------------------

Logins, settings, caches and the game data live under %LOCALAPPDATA%\eve-skills, not next to
the .exe. Upgrading means replacing eve-skills.exe; nothing you set up is lost.


Windows SmartScreen / antivirus
-------------------------------

The executable is not code-signed, so on first run Windows may say "Windows protected your PC".
Choose "More info" and then "Run anyway". The SHA-256 checksum of every release zip is published
next to it on the GitHub release page, and the build itself runs in the open on GitHub Actions
(.github/workflows/release-windows.yml).


License: GPL-3.0-only, see LICENSE.
