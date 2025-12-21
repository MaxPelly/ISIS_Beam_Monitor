Very basic python scripts to monitor the status of the STFC ISIS Pulsed Neutron and Muon Source and send notifications via temas webhooks.
Currently implemented:
- Notify on new MCR news published

Requires configuration of teams workflows to triiger post on incoming webhook and entering the relevant webhook urls in the config file.

Scripts are run as:
```
python3 <script> <config file location> <args>
```

--help lists available args
