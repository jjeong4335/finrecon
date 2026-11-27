.PHONY: fixtures demo retail test report dashboard up down logs rebuild-data rebuild-retail backup
fixtures:      ## regenerate offline fixtures + ground truth from seed/scenarios.py
	python -m seed.generate_offline
demo:          ## local run on SQLite (data/finrecon.db)
	python -m finrecon.pipeline demo
retail:        ## download + load UCI Online Retail II (needs internet)
	python -m finrecon.ingestion.uci_retail all
test:
	python -m pytest
report:        ## static HTML snapshot -> reports/snapshot.html
	python -m finrecon.pipeline report
dashboard:      ## Django app on http://127.0.0.1:8000
	python manage.py runserver
up:
	docker compose up -d --build
down:
	docker compose down
logs:
	docker compose logs -f --tail=100
rebuild-data:
	docker compose run --rm pipeline
rebuild-retail:
	docker compose run --rm retail
backup:
	docker compose exec -T db pg_dump -U finrecon_owner finrecon | gzip > backup-$$(date +%Y%m%d).sql.gz
