# Bitcoinlighthouse App Store für umbrelOS

Zwei kleine Apps, die Backups von umbrelOS zuverlässiger machen. Installation ohne Terminal:
**App Store → ⋯ → Community App Stores** und `https://github.com/cypherpunk21/netzlaufwerk-waechter`
hinzufügen.

| App | Wofür |
|---|---|
| [Netzlaufwerk-Wächter](#netzlaufwerk-wächter) | Hängt abgerissene Netzwerkfreigaben (SMB) automatisch neu ein, z. B. für Backups auf ein NAS |
| [Backup-Spiegel](#backup-spiegel) | Kopiert das Umbrel-Backup von der USB-Platte jede Nacht per SFTP auf eine Hetzner Storage Box |

---

# Netzlaufwerk-Wächter

Eine kleine Umbrel-App, die abgerissene Netzwerkfreigaben (SMB) wieder in Gang bringt.
Gedacht für Backups auf ein NAS, einen zweiten Umbrel oder eine Storage Box: Nach einem
IP-Wechsel des Routers oder einem kurzen Netzausfall läuft das Backup wieder von selbst,
ohne Neustart und ohne Terminal.

## Das Problem

Reißt die SMB-Verbindung ab, bleibt im Kernel ein toter Mount zurück. umbrelOS prüft jede
Minute mit `mountpoint`, ob die Freigabe eingehängt ist
(`packages/umbreld/source/modules/files/network-storage.ts`). `mountpoint` greift dabei auf
den Pfad zu, bekommt `EHOSTDOWN` und meldet „nicht eingehängt“. umbrelOS will daraufhin neu
einhängen, scheitert aber schon am `mkdir` auf dem toten Pfad. Beim Aufräumen hängt es den
toten Mount nicht aus, weil `mountpoint` ihn ja nicht als eingehängt erkennt. Ergebnis:
„host is down“ im Backup, „nicht verbunden“ in Dateien, bis zum Neustart.

## Was der Wächter macht

1. Er liest jede Minute die Mount-Tabelle des Systems, ohne die Pfade anzufassen.
2. Jede SMB-Freigabe im Netzwerkordner von umbrelOS muss innerhalb von 20 Sekunden ein
   Verzeichnis-Listing liefern. Das stößt nebenbei auch die automatische Wiederverbindung
   des Kernels an.
3. Antwortet eine Freigabe dreimal in Folge nicht, hängt er sie aus (`umount -l`).
4. Den Rest erledigt umbrelOS: Seine Minutenschleife hängt die Freigabe wieder ein, sobald
   der Server erreichbar ist. Das nächste stündliche Backup läuft normal.

Die Statusseite (App öffnen) zeigt den Zustand jeder Freigabe und die letzten Ereignisse.
Kommt eine Freigabe nach 10 Minuten nicht zurück, steht dort ein Hinweis. Dann liegt es
nicht mehr am toten Mount, sondern am Weg zum Server (z. B. Port 445 gesperrt).

## Installation auf dem Umbrel (ohne Terminal)

1. **App Store** öffnen → oben rechts **⋯** → **Community App Stores**.
2. `https://github.com/cypherpunk21/netzlaufwerk-waechter` einfügen und hinzufügen.
3. Im neuen Store **Netzlaufwerk-Wächter** installieren.
4. App öffnen: Die Statusseite listet die eingehängten Freigaben.

## Testen

Zum Testen reicht eine beliebige SMB-Freigabe, auch eine vom Laptop im Heimnetz.

1. Freigabe in umbrelOS hinzufügen und im Wächter prüfen, dass sie als **Verbunden** steht.
2. Ausfall simulieren, z. B. per SSH auf dem Umbrel den SMB-Verkehr sperren:
   ```sh
   sudo iptables -I OUTPUT -p tcp --dport 445 -j DROP
   ```
   Alternativ das Netzwerkkabel des Servers ein paar Minuten abziehen.
3. Nach etwa 3–4 Minuten meldet der Wächter **Tote Verbindung ausgehängt**.
4. Sperre wieder aufheben:
   ```sh
   sudo iptables -D OUTPUT -p tcp --dport 445 -j DROP
   ```
5. Innerhalb von etwa einer Minute steht im Wächter **umbrelOS hat … wieder eingehängt**, und in
   Dateien ist die Freigabe wieder verbunden. **Einstellungen → Backups → Jetzt sichern** prüft,
   ob das Backup durchläuft.

Wer erst nur beobachten will: In `docker-compose.yml` `DRY_RUN: "true"` setzen. Dann meldet der
Wächter nur, was er tun würde.

## Grenzen

- Ist der Server dauerhaft nicht erreichbar (z. B. weil ein VPN oder der Provider Port 445
  sperrt), kann niemand einhängen. Der Wächter zeigt das dann an.
- Apps, denen du in umbrelOS 2.x einen Ordner auf der Netzwerkfreigabe zugewiesen hast, sehen
  nach dem Neueinhängen möglicherweise erst nach einem Neustart der App wieder die Freigabe.
- Der Container läuft mit `privileged: true` und `pid: host`, weil er Mounts auf dem System
  aushängen muss. Er liest keine Dateien und ändert keine Daten. Der ganze Code steckt in
  einer Datei (`image/waechter.py`, nur Python-Standardbibliothek), damit man ihn prüfen kann.

## Neue Version veröffentlichen

1. Version an drei Stellen anheben, beim Wächter: `VERSION` in `image/waechter.py`, `version` in
   `lighthouse-netzlaufwerk-waechter/umbrel-app.yml` und den Image-Tag in
   `lighthouse-netzlaufwerk-waechter/docker-compose.yml`. Beim Backup-Spiegel entsprechend in
   `image-backup-spiegel/backup_spiegel.py` und `lighthouse-backup-spiegel/`.
2. Auf `main` pushen. Die GitHub-Action prüft, dass die Version an allen drei Stellen
   gleich ist, testet den Code und baut das Image für amd64 und arm64 nach
   `ghcr.io/cypherpunk21/netzlaufwerk-waechter`.
3. Umbrels mit installierter App bieten danach ein Update an.

Store-ID (`lighthouse`) und Name stehen in `umbrel-app-store.yml`. Wer sie ändert, muss
Ordnername, `id` in `umbrel-app.yml` und `APP_HOST` in `docker-compose.yml` mit ändern
(die App-ID muss mit der Store-ID beginnen).

---

# Backup-Spiegel

umbrelOS sichert sich verschlüsselt auf eine USB-Platte. Der Backup-Spiegel kopiert dieses Backup
jede Nacht auf eine Hetzner Storage Box oder einen anderen SFTP-Server. So liegt eine Kopie zu Hause
und eine außer Haus.

- **Funktioniert hinter jedem VPN und Router:** SFTP über Port 23, ausgehend. Jeder Abgleich baut eine
  frische Verbindung auf, nichts bleibt dauerhaft eingehängt.
- **Bleibt verschlüsselt:** Kopiert wird das fertige, von umbrelOS verschlüsselte Backup, 1:1. Die
  Storage Box sieht nur unlesbare Blöcke.
- **Ein Formular statt vieler Einstellungen:** Server, Benutzer, Passwort. Was gesichert wird und wie
  viele Stände es gibt, regelt weiterhin umbrelOS.

## Einrichten

1. In umbrelOS unter **Einstellungen → Backups** ein Backup auf eine USB-Platte einrichten.
2. Bei Hetzner in der Storage Box **SSH-Support** und **Externe Erreichbarkeit** einschalten.
   Empfehlung: einen **Sub-Account** nur für dieses Backup anlegen, beschränkt auf ein eigenes
   Verzeichnis.
3. Backup-Spiegel öffnen, Server (z. B. `u123456.your-storagebox.de`), Benutzer und Passwort
   eintragen, Port `23` lassen, **Speichern und Verbindung testen**.
4. Mit **Jetzt abgleichen** gleich die erste Kopie anstoßen. Danach läuft der Abgleich jede Nacht um
   die eingestellte Uhrzeit (Standard 03:30). Der erste Abgleich dauert je nach Größe und Upload
   Stunden, danach werden nur Änderungen übertragen.

Beim ersten Verbindungstest speichert die App den Hostschlüssel der Storage Box. Danach verweigert
sie jede Verbindung zu einem Server mit anderem Schlüssel.

## Wiederherstellen

1. Eine USB-Platte mit genug freiem Platz anschließen.
2. Im Backup-Spiegel unter **Zurückholen** auf **Backups auf der Storage Box anzeigen** klicken,
   Backup und Platte wählen, **Zurückholen**.
3. Die App legt auf der Platte den Ordner „Von der Storage Box zurückgeholt …“ an. In umbrelOS unter
   **Einstellungen → Backups → Wiederherstellen** (oder bei der Neuinstallation) diese Platte und
   diesen Ordner auswählen und mit dem Backup-Passwort wiederherstellen.

Bei einem Totalausfall des Umbrel geht das auch ohne die App: Den Ordner per SFTP (z. B. mit
FileZilla, Port 23) von der Storage Box auf eine Platte kopieren und in umbrelOS von dort
wiederherstellen.

## Gut zu wissen

- **Spiegel, kein Archiv:** Löscht umbrelOS alte Stände, verschwinden sie beim nächsten Abgleich auch
  auf der Storage Box. Als Schutz gegen versehentliches Löschen oder einen Angreifer mit Zugriff auf
  den Umbrel am besten bei Hetzner **automatische Snapshots** der Storage Box einschalten.
- **Abgezogene Platte:** Findet die App kein Backup auf einer USB-Platte, gleicht sie nicht ab und
  lässt die Kopie auf der Storage Box unverändert.
- **Laufendes Backup:** Sichert umbrelOS gerade, wartet der Abgleich bis zu 30 Minuten, bis es fertig
  ist.
- **Upload-Limit:** Bei langsamen Anschlüssen z. B. `2M` eintragen, damit der Upload die Leitung nicht
  blockiert.
- **Rechte:** Die App läuft mit `privileged: true` und `pid: host`, weil sie über das Dateisystem des
  Systems auf die USB-Platten zugreift. Der Code steckt in einer Datei
  (`image-backup-spiegel/backup_spiegel.py`, Python-Standardbibliothek plus rclone).

## Ende-zu-Ende-Test

Die GitHub-Action testet jeden Stand gegen einen echten SFTP-Server: Verbindungstest, Abgleich,
Nachziehen von Änderungen, Schutz bei abgezogener Platte, Warten auf ein laufendes Backup,
Zurückholen, falsches Passwort und geänderter Hostschlüssel. Lokal:

```sh
BACKUP_SPIEGEL_E2E="host:port:benutzer:passwort:/home/benutzer" python3 -m unittest tests.test_backup_spiegel_e2e -v
```

---

## Entwicklung

```sh
python3 -m unittest discover -s tests -v
```

Einstellungen des Wächters über Umgebungsvariablen: `CHECK_INTERVAL` (60), `CHECK_TIMEOUT` (20),
`FAIL_THRESHOLD` (3), `REMOUNT_WARN` (600), `NETWORK_ROOT` (`/home/umbrel/umbrel/network`),
`DRY_RUN` (false).

Der eigentliche Fix gehört in umbrelOS: `#isMounted` sollte die Mount-Tabelle lesen statt den
Pfad anzufassen, und ein eingehängter, aber toter Mount sollte vor dem Neueinhängen mit
`umount -l` gelöst werden (siehe https://github.com/getumbrel/umbrel/issues/2318). Bis das
upstream passiert, überbrückt der Wächter die Lücke.
