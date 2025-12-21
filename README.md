Very basic python scripts to monitor the status of the STFC ISIS Pulsed Neutron and Muon Source and send notifications via teams webhooks.

Currently implemented:
- Notify on new MCR news published (mcr_news.py)
- Notify on significant change in beam strength (beam_state.py)
- Notify on start and end of pearl experiment run (beam_state.py)

Note beam_state.py is functional but needs work to support other instruments and for general resilience. This is (slowly) in progress. 

Requires configuration of teams workflows to trigger post on incoming webhook and entering the relevant webhook urls in the config file.

Scripts are run as:
```
python3 <script> <config file location> <args>
```

--help lists available args
