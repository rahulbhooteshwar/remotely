# Remotely - development commands

.PHONY: help setup run doctor test binary lint build install uninstall clean

help:
	@echo "Remotely - development commands"
	@echo ""
	@echo "  make setup       install dependencies (uv sync)"
	@echo "  make run         run the app from source"
	@echo "  make doctor      check the environment"
	@echo "  make test        run the test suite"
	@echo "  make binary      build the self-contained binary"
	@echo "  make build       build the wheel and sdist"
	@echo "  make install     install as a uv tool from this checkout"
	@echo "  make uninstall   remove the uv tool"
	@echo "  make clean       remove build artefacts"
	@echo ""

setup:
	uv sync

run:
	uv run remotely

doctor:
	uv run remotely --doctor

test:
	uv run pytest -q

binary:
	uv run pyinstaller --noconfirm --clean remotely.spec
	@echo "Built: ./dist/remotely"
	@ls -lh dist/remotely

lint:
	uv run python -m compileall -q src/remotely

build:
	uv build

install:
	uv tool install --force .

uninstall:
	uv tool uninstall remotely

clean:
	rm -rf dist/ build/ .pytest_cache/ *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true
