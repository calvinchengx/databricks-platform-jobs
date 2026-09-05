# Windows-safe: recipes call Python scripts. No pipes, rm, &&, or backticks.

ifeq ($(OS),Windows_NT)
  SHELL := sh.exe
  .SHELLFLAGS := -c
endif

UV ?= uv

# PRODUCT IS A PATH, NOT A NAME. This Makefile contains no product identifier,
# which is the property that makes "a second product can use this platform
# unchanged" a fact rather than an aspiration -- the same contract
# fabric-platform-airflow3 has carried since it was built.
#
# `uv run --directory` rather than `cd &&`: the recipes here must survive
# cmd.exe, where `&&` is not available, and test_makefile_survives_cmd_exe
# fails on one. It also puts the product's own venv and its own working
# directory under the step, so its outputs land in the product rather than
# scattered through the platform.
# `./product` is an empty, gitignored mount point -- clone or symlink a product
# there, or pass PRODUCT=../my-product. Naming a default product here would put
# a Contoso identifier in the platform, which is the thing this is avoiding.
PRODUCT ?= ./product
STEP := $(UV) run --directory $(PRODUCT) --frozen

.PHONY: help doctor up down config token verify test lint witness logs

help:
	@echo "  doctor   Check prerequisites"
	@echo "  up       Start databricks-emulator + Sail + UC + OpenMetadata"
	@echo "  down     Stop the stack"
	@echo "  config   Show the resolved compose config (proves the pin)"
	@echo "  token    Put the workspace credential where the product reads it"
	@echo "  verify   Provision, ingest, bronze, silver, gold, govern"
	@echo "  test     Repo-boundary tests (no Docker)"

doctor:
	$(UV) run --frozen --group dev python scripts/doctor.py

up:
	$(UV) run --frozen --group dev python scripts/compose.py up -d --wait

down:
	$(UV) run --frozen --group dev python scripts/compose.py down -v

logs:  ## Follow the stack's logs (SVC=<service> to narrow)
	$(UV) run --frozen --group dev python scripts/compose.py logs -f --tail 100 $(SVC)

config:
	$(UV) run --frozen --group dev python scripts/compose.py config

token:  ## Put the workspace credential where the product can read it
	$(UV) run --frozen --group dev python scripts/compose.py cp databricks:/data/admin.pat $(PRODUCT)/data/admin.pat

# THE PIN THE PLATFORM CANNOT SEE. versions.env pins the emulator IMAGE; the
# product pins the client WHEEL, and since the split those live in two
# repositories. A binary and a client that disagree about the contract is the
# one mismatch a consumer repository exists to notice, so the check runs
# against whatever product this platform was actually pointed at -- before any
# step does work that a mismatch would invalidate.
verify: doctor token
	$(UV) run --frozen --group dev python scripts/check_product_pin.py $(PRODUCT)
	$(STEP) --group engine python steps/provision.py
	$(STEP) --group engine python steps/seed_secrets.py
	$(STEP) --group engine python steps/ingest.py
	$(STEP) --group engine python steps/bronze.py
	$(STEP) --group engine python steps/silver.py
	$(STEP) --group engine python steps/register.py
	$(STEP) --group dbt python steps/gold.py
	$(STEP) --group engine python steps/govern.py

witness: verify ## The family's one word for `verify`: run the cell, fail if it fails

test:
	$(UV) run --frozen --group dev python -m pytest tests -q

lint:
# platform/ went to contoso-data-product-databricks-jobs when this repo became
# a platform; its steps are linted there. scripts and tests are what is left.
	$(UV) run --frozen --group dev python -m ruff check tests scripts
