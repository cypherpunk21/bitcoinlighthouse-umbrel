FROM rclone/rclone:1.68 AS rclone

FROM python:3.12-slim-bookworm

# ssh-keyscan holt beim ersten Verbindungstest den Hostschlüssel der Storage Box,
# tzdata sorgt dafür, dass die Uhrzeit für den nächtlichen Abgleich stimmt.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
COPY backup_spiegel.py /app/backup_spiegel.py

ENV PYTHONUNBUFFERED=1 TZ=Europe/Berlin
EXPOSE 8080
CMD ["python3", "/app/backup_spiegel.py"]
