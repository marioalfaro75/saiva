# Saiva — common container operations. Run `make help` for the list.
#
# Every container target below names its compose file explicitly. That is not tidiness.
# Both compose files declare `name: saiva` and both declare the same db_data and
# db_backups volumes, so they address the *same* containers and the *same* volumes —
# they only disagree about where the images come from. Targets used to leave the file to
# compose's default resolution, which picks docker-compose.yml, the local-build dev file.
# On the production box that still reached the live stack: `up`/`restart` replaced the
# released GHCR images with a build of whatever happened to be checked out, and dropped
# the prod file's environment and volumes with it -- including the update directory the
# in-app button writes into, so the button would stop working with no visible cause.
#
# So the plain names (up down destroy logs ps seed) act on docker-compose.prod.yml, and
# only the dev-* names build from source. The production box is the one machine where
# getting this wrong costs data, so the habitual name is the safe one there: a developer
# who types `make up` out of habit gets a GHCR pull and notices immediately, while an
# operator who types `make up` out of habit gets what they meant. There is no flag to
# remember, because a forgotten flag is exactly what made the old `make restart`
# dangerous. down/logs/ps reach the same containers under either file anyway; they use
# the prod file so that nothing in the production stack is silently skipped.
#
# `deploy` and `pull` sit outside that scheme because they delegate to scripts/deploy.sh,
# which picks the file itself from its own --pull flag; their help lines name the file so
# the difference is readable rather than implied.
#
# COMPOSE stays overridable for hosts that only have the legacy binary:
# `make up COMPOSE=docker-compose`.
COMPOSE ?= docker compose
COMPOSE_PROD := $(COMPOSE) -f docker-compose.prod.yml
COMPOSE_DEV  := $(COMPOSE) -f docker-compose.yml

# Lets `make destroy SAIVA_CONFIRM_DESTROY=destroy-saiva-data` work as well as setting it
# in the environment; make does not pass its own variables to recipes otherwise.
export SAIVA_CONFIRM_DESTROY

.PHONY: help deploy pull update up down destroy restart logs ps seed dev-up dev-rebuild check

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-13s\033[0m %s\n", $$1, $$2}'

deploy: ## Dev file: build from source, bootstrap .env, start, wait (SEED/LAN/SITE=<addr>)
	@./scripts/deploy.sh $(if $(filter 1,$(SEED)),--seed,) $(if $(filter 1,$(LAN)),--lan,) $(if $(SITE),--site $(SITE),)

pull: ## Prod file: pull the GHCR images and start, no local build (same SEED/LAN/SITE)
	@./scripts/deploy.sh --pull $(if $(filter 1,$(SEED)),--seed,) $(if $(filter 1,$(LAN)),--lan,) $(if $(SITE),--site $(SITE),)

# The one name everything points at: the in-app button, the docs and this target all
# reach scripts/deploy.sh --apply-release. There is deliberately no second copy of the
# sequence, because three copies of it is what the old design had and they delivered
# different subsets of a release. See docs/UPDATING.md.
update: ## Apply the newest release — tree, config and images — gated, rolled back if it fails
	@./scripts/deploy.sh --apply-release

up: ## Start the prod stack from the images already present — no build, no pull
	$(COMPOSE_PROD) up -d

down: ## Stop the prod stack, database preserved
	$(COMPOSE_PROD) down

# ONE command, joined with `&&`, and that is not a style choice. As two recipe lines,
# make checks each line's status -- but `make -i`, `make -k` and a leading `-` all tell
# make to carry on past a failed line, so a refused confirmation would be followed by
# `down -v` anyway. Verified: `make -i destroy </dev/null` printed "Aborted. Nothing was
# deleted." and then ran the destructive command. With `&&` the refusal is part of the
# same command and there is no line for make to continue past.
destroy: ## Wipe the database AND every pre-migration backup (requires a typed word)
	@./scripts/confirm-destroy.sh && $(COMPOSE_PROD) down -v

check: ## Run everything CI runs, with the same invocations
	@./scripts/check-all.sh

logs: ## Follow the API logs
	$(COMPOSE_PROD) logs -f api

ps: ## Show service status
	$(COMPOSE_PROD) ps

seed: ## Load demo data into a running stack
	$(COMPOSE_PROD) exec -T api python -m app.services.seed

dev-up: ## Dev file: build from this working tree and start — bare compose, no health wait
	$(COMPOSE_DEV) up -d --build

dev-rebuild: ## Dev file: rebuild api+web from this working tree and restart just those two
	$(COMPOSE_DEV) up -d --build api web

# `restart` used to be `up -d --build api web` with no -f, so on a pull-based box it
# replaced the released images with a build of the checked-out tree — a silent downgrade
# to an unreleased, untested pair. There is no safe reading of the old name, so it is
# gone rather than redefined: a target that errors and says where to go is recoverable,
# and a target that quietly means something else is not. No `##`, so it stays out of help.
restart:
	@echo "make restart is gone: it rebuilt api+web from source, which on a pulled" >&2
	@echo "deployment replaces the released images with a local build. Use instead:" >&2
	@echo "  make update       apply the release pinned in .env (production)" >&2
	@echo "  make up           restart from the images already present (production)" >&2
	@echo "  make dev-rebuild  rebuild api+web from this working tree (development)" >&2
	@exit 1
