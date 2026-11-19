FROM python:3.12-slim

# nsenter (aus util-linux) führt Befehle im Mount-Namespace des Hosts aus.
RUN apt-get update \
    && apt-get install -y --no-install-recommends util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY waechter.py /app/waechter.py

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python3", "/app/waechter.py"]
