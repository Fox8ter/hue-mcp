# Dockerfile
FROM python:3.12-slim

# OS deps (optionnel mais utile pour debug/ssl)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata curl && rm -rf /var/lib/apt/lists/*

# Dossier d'app & conf Hue (le script stocke dans ~/.hue-mcp)
WORKDIR /app
RUN mkdir -p /root/.hue-mcp

# Installer les deps Python annoncées dans le README
# (phue pour Hue local + mcp pour le protocole)
RUN pip install --no-cache-dir phue mcp

# Copier le serveur
COPY hue_server.py /app/hue_server.py

# Variables utiles : host/port MCP et IP du bridge (optionnelle)
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8888
# Optionnel : fixer l’IP du bridge pour éviter l’auto-discovery
# ENV BRIDGE_IP=192.168.1.10

EXPOSE 8888

# Lancer le serveur (log plus verbeux si besoin)
CMD ["bash", "-lc", "python /app/hue_server.py --host ${MCP_HOST} --port ${MCP_PORT}"]
