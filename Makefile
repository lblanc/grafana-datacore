.PHONY: help up down logs build test once

help:
	@echo "Targets: build | up | down | logs | once | test"

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

# Run a single collection cycle locally (without Docker), useful when
# debugging filters. Requires Python 3.11+ and the env vars from .env.
once:
	cd collector && DATACORE_CONFIG=collector.ini python collector.py --once --log-level DEBUG

test:
	cd collector && python -m pytest -q
