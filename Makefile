.PHONY: help install test scan

help:
	@echo "AgentLoom Runtime targets:"
	@echo "  install  - pip install -e .[dev]"
	@echo "  test     - run pytest"
	@echo "  scan     - OSS internal-info scan (requires envistor-data scanner)"

install:
	pip install -e .[dev]

test:
	python -m pytest -q

scan:
	python ../../envistor-data/Scripts/oss_release/scan_internal_info.py . \
		--allowlist ../../envistor-data/Scripts/oss_release/allowlist.txt
