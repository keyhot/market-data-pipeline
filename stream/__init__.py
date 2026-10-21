"""Stream operations (#43): the watchdog, the OBS scene spec and the soak report.

Promoted out of scripts/ so the logic is importable like director/ and
broadcast/; the scripts/ files that systemd and the docs invoke are thin
entry points over these modules. Importing the package runs nothing.
"""
