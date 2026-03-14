# ─────────────────────────────────────────────────────────────────────────────
# DalyBMS — Makefile
# Gestion de l'infrastructure Docker (Mosquitto · InfluxDB · Grafana · Node-RED)
# ─────────────────────────────────────────────────────────────────────────────

COMPOSE = docker compose -f docker-compose.infra.yml --env-file .env.docker

.PHONY: help up down restart logs status ps reset influx-token

help: ## Afficher cette aide
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ── Cycle de vie ──────────────────────────────────────────────────────────────

up: check-env ## Démarrer tous les services en arrière-plan
	$(COMPOSE) up -d
	@echo ""
	@echo "  Grafana   → http://localhost:3001"
	@echo "  InfluxDB  → http://localhost:8086"
	@echo "  Node-RED  → http://localhost:1880"
	@echo "  MQTT      → localhost:1883"

down: ## Arrêter tous les services (données préservées)
	$(COMPOSE) down

restart: ## Redémarrer tous les services
	$(COMPOSE) restart

# ── Monitoring ────────────────────────────────────────────────────────────────

logs: ## Suivre les logs de tous les services (Ctrl+C pour quitter)
	$(COMPOSE) logs -f

logs-%: ## Logs d'un service : make logs-grafana
	$(COMPOSE) logs -f $*

ps: ## État des containers
	$(COMPOSE) ps

status: ps ## Alias de ps

# ── Maintenance ───────────────────────────────────────────────────────────────

pull: ## Mettre à jour les images Docker
	$(COMPOSE) pull

reset: ## ⚠ Supprimer containers ET volumes (perte de données)
	@echo "⚠  Ceci supprime toutes les données (InfluxDB, Grafana, Node-RED...)"
	@read -p "Confirmer ? [y/N] " ans && [ "$$ans" = "y" ]
	$(COMPOSE) down -v

influx-token: ## Afficher le token InfluxDB actuel
	@docker exec dalybms_influxdb influx auth list --hide-headers 2>/dev/null \
		|| echo "InfluxDB non démarré ou token déjà dans .env.docker"

# ── Vérifications ─────────────────────────────────────────────────────────────

check-env:
	@test -f .env.docker || (echo "❌  Fichier .env.docker manquant. Faire : cp .env.docker.example .env.docker" && exit 1)
