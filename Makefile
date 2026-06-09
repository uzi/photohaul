.PHONY: test help

help:
	@echo "make test   - run the test suite (stdlib unittest, zero dependencies)"

# Run the full test suite. No dependencies to install.
test:
	python3 -m unittest discover -s tests -v
