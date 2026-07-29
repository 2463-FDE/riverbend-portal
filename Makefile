.PHONY: up down logs ps build seed seed-gen psql test frontend-dev config

up:            ## start the whole stack
	docker compose up -d

down:          ## stop the stack
	docker compose down

logs:          ## tail logs
	docker compose logs -f

ps:            ## service status
	docker compose ps

build:         ## build all images
	docker compose build

seed:          ## load schema + demo data (re-runs against a running db)
	docker compose exec -T postgres psql -U $${DB_USER:-riverbend_app} -d $${DB_NAME:-riverbend} < db/schema.sql
	docker compose exec -T postgres psql -U $${DB_USER:-riverbend_app} -d $${DB_NAME:-riverbend} < db/seed/seed.sql

seed-gen:      ## regenerate db/seed/seed.sql from the generator (deterministic)
	python3 db/seed/generate_seed.py > db/seed/seed.sql

psql:          ## open a psql shell
	docker compose exec postgres psql -U $${DB_USER:-riverbend_app} -d $${DB_NAME:-riverbend}

test:          ## run unit tests (no infra needed, no AWS, zero spend)
	pip install -r requirements-dev.txt >/dev/null
	pytest -m "not integration" -q

test-live:     ## run the key-gated Bedrock smoke tests -- THIS SPENDS MONEY
	pip install -r requirements-dev.txt >/dev/null
	pip install -r services/ai-orchestrator/requirements.txt >/dev/null
	pytest --live -m live -q -s

frontend-dev:  ## run the Next.js dev server
	cd frontend && npm install && npm run dev

test-ui:       ## run the frontend component tests (fast, no stack needed)
	cd frontend && npm install --silent && npm test

test-e2e:      ## run the browser journeys -- REQUIRES `make up` first
	cd frontend && npm install --silent && npx playwright install --with-deps chromium && npm run test:e2e

config:        ## validate the compose file
	docker compose config -q && echo "compose OK"
