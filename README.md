Very basic python scripts to monitor the status of the STFC ISIS Pulsed Neutron and Muon Source and send notifications via teams webhooks.

Currently implemented:
- Notify on new MCR news published (mcr_news.py)
- Notify on significant change in TS1, TS2, or Muon beam current (beam_state.py)
- Notify on start of a new PEARL experiment run and when counts reach a target threshold (beam_state.py)

`beam_state.py` connects to the ISIS data WebSocket for real-time updates and will automatically reconnect if the connection is lost.

Requires configuration of teams workflows to trigger post on incoming webhook and entering the relevant webhook urls in the config file. Copy `config.ini.example` to `config.ini` as a starting point. There are three separate webhook channels:
- `news_teams_url` – MCR news updates (mcr_news.py)
- `beam_teams_url` – beam current state changes (beam_state.py)
- `experiment_teams_url` – experiment run start and counts threshold notifications (beam_state.py)

Scripts are run as:
```
python3 <script> <config file location> <args>
```

--help lists available args. Notable args:
- `mcr_news.py`: `-n` / `--notify_current` – send a notification for the current news immediately on start (otherwise waits for new news)
- `beam_state.py`: `-nc` / `--notify_counts` – counts threshold at which a "run about to finish" notification is sent (default: 130)


Note these are just some quick scripts pulled together on a beamtime that I have found useful. No promises they are well written or robust.