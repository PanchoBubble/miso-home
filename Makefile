PYTHON ?= python3
PYTHONPATH := src

.PHONY: check-config integration-test run test

test:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests/unit -p 'test_*.py'

integration-test:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests/integration -p 'test_*.py'

check-config:
	mkdir -p .local/db .local/state .local/models
	PYTHONPATH=$(PYTHONPATH) \
	MISO_DB_PATH=$(CURDIR)/.local/db/miso.sqlite3 \
	MISO_STATE_DIR=$(CURDIR)/.local/state \
	MISO_MODEL_DIR=$(CURDIR)/.local/models \
	$(PYTHON) -m miso --check-config

run:
	mkdir -p .local/db .local/state .local/models
	PYTHONPATH=$(PYTHONPATH) \
	MISO_DB_PATH=$(CURDIR)/.local/db/miso.sqlite3 \
	MISO_STATE_DIR=$(CURDIR)/.local/state \
	MISO_MODEL_DIR=$(CURDIR)/.local/models \
	$(PYTHON) -m miso
