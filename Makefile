# Saiva — common container operations. Run `make help` for the list.
COMPOSE ?= docker compose

.PHONY: help deploy pull seed up down destroy restart logs ps

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

deploy: ## Deploy (SEED=1 add demo · LAN=1 expose on LAN over https · SITE=<addr> custom address)
	@./scripts/deploy.sh $(if $(filter 1,$(SEED)),--seed,) $(if $(filter 1,$(LAN)),--lan,) $(if $(SITE),--site $(SITE),)

pull: ## Deploy from prebuilt GHCR images — no local build (same SEED/LAN/SITE flags)
	@./scripts/deploy.sh --pull $(if $(filter 1,$(SEED)),--seed,) $(if $(filter 1,$(LAN)),--lan,) $(if $(SITE),--site $(SITE),)

seed: ## Load demo data into a running stack
	$(COMPOSE) exec -T api python -m app.services.seed

up: ## Build and start the stack in the background
	$(COMPOSE) up -d --build

down: ## Stop the stack (database preserved)
	$(COMPOSE) down

destroy: ## Stop the stack and wipe the database volume
	$(COMPOSE) down -v

restart: ## Rebuild and restart the app after code changes
	$(COMPOSE) up -d --build api web

logs: ## Follow the API logs
	$(COMPOSE) logs -f api

ps: ## Show service status
	$(COMPOSE) ps
