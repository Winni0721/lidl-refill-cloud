# lidl-refill-cloud

Bucht bei Lidl Connect automatisch die kostenlose 1-GB-Nachbuchoption, wenn weniger als 0,3 GB übrig sind.
Läuft per GitHub Actions alle ~5 Minuten. Zugangsdaten liegen als Repository-Secrets (`LIDL_USER`, `LIDL_PASS`, `STATE_KEY`).

- Pausieren: Actions → lidl-refill → „Disable workflow“
- Testmodus: Repository-Variable `LIDL_DRY_RUN=1`
- Jede Prüfung im Log zeigen: Variable `LIDL_VERBOSE=1`

API-Aufrufe nach [majd-abed/lidl-auto-refill](https://github.com/majd-abed/lidl-auto-refill).
