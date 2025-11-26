PYTHON ?= python3
UV ?= uv

.PHONY: run lint test install

install:
	$(UV) pip install --system ".[dev]" || $(PYTHON) -m pip install -e .[dev]

run:
	PYTHONPATH=src $(PYTHON) -m voicelive_sip_gateway.gateway.main

lint:
	PYTHONPATH=src $(PYTHON) -m ruff check src tests
	PYTHONPATH=src $(PYTHON) -m mypy src/voicelive_sip_gateway

test:
	PYTHONPATH=src $(PYTHON) -m pytest
