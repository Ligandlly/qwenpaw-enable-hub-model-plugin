PYTHON ?= python3

.PHONY: test compile bundle verify clean

test:
	$(PYTHON) -m pytest -q

compile:
	$(PYTHON) -m compileall -q enable_hub_model.py tests scripts

bundle:
	$(PYTHON) scripts/bundle.py

verify: bundle
	@for zip in dist/*.zip; do $(PYTHON) scripts/verify_offline.py "$$zip"; done

clean:
	rm -rf dist __pycache__ .pytest_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
