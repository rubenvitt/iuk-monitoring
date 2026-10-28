# iuk-monitoring

Server-Monitoring für eine kleine Ehrenamts-Infrastruktur: ein [Beszel](https://beszel.dev)-Dashboard für einen Server im Fahrzeug und einen Cloud-Server, Telegram-Alarme und `fuekw-watch`, ein Wächter im Fahrzeug für das Telekom-Datenvolumen und die Erreichbarkeit des Hubs.

- `fuekw/`: Wächter (Python, nur Standardbibliothek), systemd-Units, Beszel-Agent
- `iuk-services/`: Beszel-Hub und Agent als Compose-Projekt

Alle umgebungsspezifischen Werte (Adressen, Tokens) kommen aus `.env`-Dateien, Vorlagen liegen als `.env.example` daneben. Geheimnisse gehören nie ins Repo.

## Tests

```sh
python3 -m unittest discover -s fuekw -v
```
