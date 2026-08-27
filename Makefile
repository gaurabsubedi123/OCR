# Shortcuts. Everything here also works as a plain command — see README.md.

VENV := .venv
PY   := $(VENV)/bin/python

.PHONY: install ui test doctor clean

install:            ## create the venv and install the tool
	uv venv || python3 -m venv $(VENV)
	uv pip install -e ".[dev]" || $(PY) -m pip install -e ".[dev]"

ui:                 ## start the local web interface on http://127.0.0.1:5000
	$(PY) -m ocrtool.cli ui

test:               ## run the test suite
	$(PY) -m pytest -q

doctor:             ## check that tesseract and the dependencies are present
	$(PY) -m ocrtool.cli doctor

clean:              ## remove caches (never touches an output folder)
	rm -rf .pytest_cache **/__pycache__ ocrtool/__pycache__ ocrtool/web/__pycache__ tests/__pycache__
