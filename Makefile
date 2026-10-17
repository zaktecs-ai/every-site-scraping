# Every-Site Scraping — developer tasks.
#
#   make install    install runtime + dev dependencies
#   make test       run the full suite
#   make lint       ruff, if installed
#   make check      lint + tests (what CI runs)
#   make smoke      a real end-to-end run against example.com
#   make build      build the wheel/sdist
#   make docker     build the container image
#   make clean      remove generated artefacts

PY ?= python3
PIP ?= $(PY) -m pip

.PHONY: help install test lint check smoke build docker clean schema

help:
	@grep -E '^#   ' Makefile | sed 's/^#   //'

install:
	$(PIP) install -r requirements.txt
	$(PIP) install psutil

dev: install
	$(PIP) install coverage ruff build

test:
	$(PY) -m unittest discover -v

lint:
	-ruff check .

check: lint test

smoke:
	$(PY) cli.py --version
	$(PY) cli.py schema | tail -1
	$(PY) cli.py scrape https://example.com https://www.iana.org -o /tmp/smoke.csv
	$(PY) cli.py audit /tmp/smoke.csv
	@rm -f /tmp/smoke.csv /tmp/smoke.csv.*

schema:
	$(PY) cli.py schema

build:
	$(PIP) install build
	$(PY) -m build

docker:
	docker build -t every-site-scraping:local .

clean:
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete
	rm -f out.csv* output.csv* smoke.csv* /tmp/smoke.csv*
