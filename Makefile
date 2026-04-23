URL ?= https://example.com

.PHONY: growth

growth:
	python3 tools/growth_agent.py "$(URL)"
