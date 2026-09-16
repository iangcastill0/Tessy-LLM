# Tessy - common tasks.
.DEFAULT_GOAL := help
PYTHON ?= python3
VENV   ?= .venv
BIN    := $(VENV)/bin
DB     ?= data/output/case_index.db

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

$(BIN)/python:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --quiet --upgrade pip

.PHONY: install
install: $(BIN)/python ## Install the package
	$(BIN)/pip install --quiet -e .

.PHONY: install-dev
install-dev: $(BIN)/python ## Install with development dependencies
	$(BIN)/pip install --quiet -e ".[dev]"

.PHONY: tesseract
tesseract: ## Build and install Tesseract from source
	./scripts/build_tesseract.sh

.PHONY: doctor
doctor: ## Check that tesseract and the Python deps are wired up
	$(BIN)/tessy doctor

.PHONY: gui
gui: ## Launch the desktop application
	$(BIN)/tessy-gui $(DB)

.PHONY: test
test: ## Run the full test suite (desktop tests need a display)
	$(BIN)/pytest -q

.PHONY: test-gui
test-gui: ## Run the suite with a virtual display (Linux headless)
	xvfb-run -a $(BIN)/pytest -q

.PHONY: test-unit
test-unit: ## Run only the tests that do not need tesseract
	$(BIN)/pytest -q -m "not requires_tesseract"

.PHONY: coverage
coverage: ## Run tests with a coverage report
	$(BIN)/pytest -q --cov=tessy --cov-report=term-missing

.PHONY: lint
lint: ## Lint the code
	$(BIN)/ruff check src tests

.PHONY: format
format: ## Auto-format the code
	$(BIN)/ruff format src tests
	$(BIN)/ruff check --fix src tests

.PHONY: ingest
ingest: ## Ingest a spreadsheet: make ingest SHEET=data/input/case.xlsx
	@test -n "$(SHEET)" || { echo "usage: make ingest SHEET=path/to/case.xlsx"; exit 1; }
	$(BIN)/tessy ingest "$(SHEET)" --db "$(DB)"

.PHONY: search
search: ## Search the index: make search Q="SMITH"
	@test -n "$(Q)" || { echo 'usage: make search Q="terms"'; exit 1; }
	$(BIN)/tessy search $(Q) --db "$(DB)"

.PHONY: clean
clean: ## Remove build and test artefacts
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache .coverage coverage.xml htmlcov
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

.PHONY: clean-all
clean-all: clean ## Also remove the virtualenv
	rm -rf $(VENV)
