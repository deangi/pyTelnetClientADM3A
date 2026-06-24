# pyTelnetClient

A telnet client for a CP/M machine, emulating an ADM-3A terminal as was common in CP/M 2.2 days.

The ADM-3A emulation is needed for programs like WordStar and other non-console applications such as SuperCalc. Version 2.0 also includes VT-100 keyboard handling, line drawing characters, inverse video, connection profiles, and automatic reconnect.

![pyTelnetClient VT-100 session](TelnetClient.png)

## Running

Run from an Anaconda prompt:

```powershell
python pyTelnetClient.py
```

## Version

Dean Gienger, 13 May 2026

Version 2.0, 24 June 2026

## Saved Connections

Saved connections are stored in:

```text
%APPDATA%\pyTelnetClient\connections.json
```
