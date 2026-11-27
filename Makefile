.PHONY: fixtures demo test report dashboard up down logs rebuild-data backup
fixtures:      ## regenerate offline fixtures + ground truth from seed/scenarios.py
	python -m seed.generate_offline
demo:          ## local run on SQLite (data/finrecon.db)
	python -m finrecon.pipeline demo
test:
	python -m pytest
report:        ## static HTML snapshot -> reports/snapshot.html
	python -m finrecon.pipeline report
dashboard:
	streamlit run dashboard/Home.py
up:
	docker compose up -d --build
down:
	docker compose down
logs:
	docker compose logs -f --tail=100
rebuild-data:
	docker compose run --rm pipeline
backup:
	docker compose exec -T db pg_dump -U finrecon_owner finrecon | gzip > backup-$$(date +%Y%m%d).sql.gz
