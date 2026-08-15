.PHONY: whisper-coreml api-types api-types-check config-docs config-docs-check format format-check

whisper-coreml:
	./scripts/setup-whisper-coreml.sh

# The web client's request and response types are generated from the application
# schema, not transcribed from the Python models. Run this after changing any
# route, or any model a route returns.
api-types:
	uv run python scripts/dump_openapi.py --write
	cd web && npm run gen:api

# The half that keeps the generation honest. A committed artifact nobody verifies
# drifts exactly like the hand-written mirror it replaced, so this fails when the
# schema on disk no longer matches the application, or when the TypeScript no
# longer matches the schema.
api-types-check:
	uv run python scripts/dump_openapi.py --check
	cd web && npm run gen:api && git diff --exit-code -- src/api-types.ts

# docs/configuration.md and .env.example are both rendered from the Settings
# model, including the reasoning written as comments above each field. Run this
# after adding or changing a setting.
config-docs:
	uv run python scripts/dump_config_reference.py --write

# .env.example was hand-maintained and had drifted: most variables the scripts
# read were missing, and four contradicted the model's defaults. This is what
# stops that happening again.
config-docs-check:
	uv run python scripts/dump_config_reference.py --check

format:
	uv run ruff format

# `ruff check` passed for months while `ruff format` did not, because the working
# rule was "format only what you touch" and nobody ran it over the whole tree.
# Four files had drifted by the time anyone looked. This is the half that makes
# the rule enforceable rather than aspirational.
format-check:
	uv run ruff format --check
