# Changelog

All notable changes to the monitor are recorded here. Versions follow
[Semantic Versioning](https://semver.org/); a release is a `vX.Y.Z` tag, and
its notes are copied from the matching section below. The push site has its
own versions and changelog; each release here notes the version its
`push_site` submodule points at.

## [Unreleased]

- The TUI header shows the daemon's version, and the TUI's own if it differs (e.g. after an update without a restart).
- Push notifications carry the daemon's version as `monitor_version`, which the push site shows in its footer.

## [1.0.0] - 2026-10-09

First versioned release. Includes push site v1.0.0.

- Beam state cards for TS1, TS2 and Muons, run tracking for any number of instruments, MCR news and a daily summary, sent as Teams Adaptive Cards and optionally to the push site.
- TUI client with live graphs, an instruments panel and a config editor.
- The daemon's version is shown at the foot of every Teams card, logged at startup and printed by `python main.py --version`.
