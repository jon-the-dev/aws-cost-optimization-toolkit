.PHONY: install lint test validate-templates samples check clean

install:
	pipenv install --dev

lint:
	pipenv run ruff check scripts tests
	pipenv run ruff format --check scripts tests

test:
	pipenv run pytest -q

validate-templates:
	pipenv run cfn-lint templates/*.yaml

samples:
	pipenv run python tests/generate_samples.py

check: lint test validate-templates

clean:
	rm -rf .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
