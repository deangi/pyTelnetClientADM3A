# pyTelnetClient

A telnet client for a 1970's 1980's machine like CP/M, MSDOS, PDP-11, emulating an ADM-3A or DEC VT100 terminal as was common in those years.

The ADM-3A emulation is needed for programs like WordStar and other non-console applications such as SuperCalc. Version 2.2 includes VT-100 keyboard handling, line drawing characters, inverse video, connection profiles, automatic reconnect, an 80×24 display, up to 1000 lines of selectable scrollback, and passes ASCII control characters (including Ctrl+C) through to the host.   The ADM3A will work with older programs from CPM and MSDOS days.   The VT100 will work with Digital Equipment Corp (DEC) programs used on PDP-11 and VAX machines.

Display is **80×24** with up to **1000 lines** of scrollback (mouse wheel or scrollbar). Select and copy work across the live screen and scrollback.

![pyTelnetClient VT-100 session](TelnetClient.png)

## Running

Run from an Anaconda prompt:

```powershell
python pyTelnetClient.py
```

## Version

Dean Gienger, 13 May 2026

Version 2.2, 7 August 2026

## Saved Connections

Saved connections are stored in:

```text
%APPDATA%\pyTelnetClient\connections.json
```
