.PHONY: help install refresh build all test lint format clean
.DEFAULT_GOAL := help

PYTHON := uv run python

help:  ## Show targets
	@awk 'BEGIN {FS = ":.*##"; printf "Targets:\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

install:  ## Install dependencies
	uv sync

refresh:  ## Read parquets from sister projects and write data/spots.json
	$(PYTHON) -m scripts.refresh_data

build:  ## Render data/spots.json into outputs/index.html
	$(PYTHON) -m scripts.build_html

all: refresh build  ## refresh + build

test:
	uv run pytest

lint:
	uv run ruff check scripts tests

format:
	uv run ruff format scripts tests

clean:
	rm -f outputs/index.html
