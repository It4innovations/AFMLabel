# AFMLabel - verification

**Document purpose:** describes how the functionality of AFMLabel is verified (strategy, test suite, static checks) and reports the results of the verification runs performed for this documentation package.
**Software version:** AFMLabel 1.0.0

## 1. Verification strategy

Verification combines automated tests at four levels, static analysis and repository consistency checks:

| Level | Purpose | Tooling |
| --- | --- | --- |
| Unit tests | Pure logic in isolation: persistence (atomic writes, locks, RLE, migrations), pairing, image decoding and display, overlap engine and validation, region operations, export rasters, settings, authentication, redaction, help, job manager, resource monitor. | pytest |
| Integration tests | The FastAPI application through its HTTP API (`fastapi.testclient`) with a temporary workspace: complete flows for authentication, projects, channels, datasets and import, regions, fill, validation, segmentation jobs, imports, exports, security, health, static assets, upgrades/compatibility and a no-network check at start-up. | pytest, httpx |
| Model contract tests | Every segmentation backend must satisfy one shared contract (`tests/model_contract/backend_contract.py`); the fake backend is checked on every run, unavailable SAM backends must report exact reasons, real SAM backends are checked only where checkpoints exist (`real_model`). | pytest |
| Browser tests | The real UI in Chromium against a live Uvicorn server: login, project and class management, import and pairing, workspace tools, channel navigator, fill tool, regions editing, overlaps and Findings, proposals, export and download, XML and lossy imports, search with 500 datasets, view controls, help and accessibility, theme. | pytest-playwright |
| Static analysis | Lint and import order (ruff), static typing (mypy). | ruff, mypy |
| Consistency checks | MIT header in every source file; generated shortcut table in the user guide up to date; privacy diagnostic (no remote assets, telemetry or runtime endpoints). | project scripts |

Design-for-test properties of the code that the suite relies on: an application factory with injectable settings (every test gets its own workspace under `tmp/pytest`), a deterministic CPU-only `fake` segmentation backend implementing the full backend contract, synthetic image fixtures written on demand (no binary test data in the repository), a replica of the downstream NanoUNet loader (`tests/fixtures/nanounet_loader.py`) and schema-version-1 documents (`tests/fixtures/schema_v1/`) for migration tests.

## 2. Test suite structure

The suite contains **266** tests (pytest collection, including parametrised cases):

| Directory | Tests | Marker | Content |
| --- | --- | --- | --- |
| `tests/unit/` | 150 | - | 22 modules: `test_access_log_filter`, `test_atomic`, `test_auth`, `test_cvat_xml`, `test_export_rasters`, `test_help`, `test_images`, `test_job_manager`, `test_license_headers`, `test_locks`, `test_manifest`, `test_migrations_compat`, `test_no_qt`, `test_overlaps`, `test_pairing`, `test_project_store`, `test_redaction`, `test_region_operations`, `test_registry_loading`, `test_resources`, `test_rle`, `test_settings` |
| `tests/integration/` | 65 | - | 16 modules: `test_auth_api`, `test_channels_api`, `test_datasets_api`, `test_demo_project_script`, `test_exports_api`, `test_fill_api`, `test_health_api`, `test_imports_api`, `test_no_network_on_startup`, `test_projects_api`, `test_regions_api`, `test_security`, `test_segmentation_api`, `test_static_assets`, `test_upgrade_api`, `test_validation_api` |
| `tests/model_contract/` | 26 | 12 of them `real_model` | `test_fake_backend` (2), `test_sam31_placeholder` (4), `test_sam3_sam21_placeholder` (8), `test_sam31_real` (3, real_model), `test_sam3_sam21_real` (9, real_model) |
| `tests/browser/` | 25 | `browser` | 14 modules: `test_channels_and_fill`, `test_dataset_card_layout`, `test_exports`, `test_findings_panel`, `test_help_accessibility`, `test_import`, `test_legacy_import`, `test_overlaps`, `test_proposals`, `test_regions_editing`, `test_search_import_export`, `test_smoke`, `test_view_controls`, `test_workspace` |

What the areas cover (the mapping of individual tests to requirements is in [Functional requirements, section 7](functional-requirements.md#7-traceability-matrix)):

- **Data integrity:** atomic replacement and interrupted writes (`test_atomic`), revision conflicts and failed mutations leaving no partial state (`test_project_store`), lossless RLE round trips including holes (`test_rle`), pixel-exact `uint16` PNG/TIFF round trips (`test_export_rasters`), deterministic composition and exactly one winner per pixel (`test_overlaps`, `test_region_operations`), persistence across application restarts (`test_projects_api`, `test_regions_api`, `test_auth_api`).
- **Scientific rules:** overlap group identity and review pruning, validation blocking/warning matrix, Boundary policy and fill never overwriting labels (`test_overlaps`, `test_validation_api`); export blocked until validated and stale after edits (`test_exports_api`); NanoUNet output read back by the loader replica (`test_nanounet_export_passes_the_downstream_loader_contract`); flattened-import rules (`test_upgrade_api`).
- **Import:** pairing-key extraction cases, order-based fallback only on opt-in, ambiguous and mismatched pairs never committed, upload type and size limits, source replacement, CVAT XML parsing including hidden smaller regions and malformed documents.
- **Security:** unauthenticated access, HttpOnly cookie, rate limiting, CSRF header, session persistence and revocation, Argon2id, 7 path-traversal variants, download scoping, secret redaction in logs and error responses, secrets rejected in YAML.
- **Robustness and operations:** resource-metric failures, low RAM/disk warnings, typed job failures and OOM handling, recovery of interrupted jobs, corrupt lock files, no network connection at start-up, no Qt dependency, licence headers, help registry and documentation anchors.
- **UI:** the browser tests drive the actual pages with mouse and keyboard and assert on the DOM and on API state (for example that marking Findings reviewed does not change any region area, or that undo restores the original region ids).

## 3. Test environment

All runs below were executed in the repository working tree with an editable install (`pip install -e ".[dev]"`) into a new virtual environment.

| Item | Value |
| --- | --- |
| OS | Microsoft Windows 11 Pro, 10.0.26200, AMD64 |
| CPU | Intel Xeon E5-2640 v3 @ 2.60 GHz, 16 physical cores / 32 logical processors |
| RAM | 255.9 GiB |
| GPU | NVIDIA GeForce RTX 2080 Ti (present but not used: PyTorch was not installed, no model checkpoints) |
| Python | 3.13.13 (64-bit, MSC v.1944) |
| Key packages | fastapi 0.162.5, starlette 1.10.0, uvicorn 0.54.0, pydantic 2.14.3, pydantic-settings 2.16.2, numpy 2.5.4, pillow 13.0.0, tifffile 2026.11.8, pycocotools 2.0.11, argon2-cffi 25.1.0, jinja2 3.1.6, psutil 7.2.2 |
| Test tooling | pytest 9.2.0, pytest-playwright 0.10.0, playwright 1.65.1 (Chromium 158.0.8127.18), httpx 0.28.1, ruff 0.17.4, mypy 2.4.1 |

## 4. Results of the automated runs

### 4.1 Summary

| Run | Command | Result | Duration |
| --- | --- | --- | --- |
| CPU-only test suite | `pytest -m "not browser and not real_model and not gpu"` | **225 passed, 4 skipped, 0 failed** (229 selected, 37 deselected) | 64.0 s (pytest) |
| Browser test suite | `pytest -m browser --browser chromium` | **25 passed, 0 failed** (241 deselected) | 120.0 s (pytest) |
| Lint | `ruff check src tests scripts` | **All checks passed** (exit 0) | < 1 s |
| Type check | `mypy src` | **Success: no issues found in 88 source files** (exit 0) | 14 s |
| Licence headers | `python scripts/add_license_headers.py --check` | **0 files without a licence header** (exit 0) | < 1 s |
| Help reference | `python scripts/generate_help_reference.py --check` | **up to date** (exit 0) | < 1 s |
| Privacy diagnostic | `python -m afmlabel.diagnostics.privacy` | **status: ok** (exit 0) | < 1 s |
| Formatting (informational) | `ruff format --check src tests scripts` | 3 files would be reformatted, 161 already formatted (exit 1) | < 1 s |

In total **250 of the 254 tests that can run without model checkpoints passed, 4 were skipped and none failed.** The 12 `real_model` tests were not executed (no PyTorch, no checkpoints on the test machine); no test carries the `gpu` marker alone.

### 4.2 Skipped tests

| Test | Reason (from pytest) | Assessment |
| --- | --- | --- |
| `integration/test_datasets_api.py::test_real_developer_data_pairs_when_available` | developer-local `.data/extracted` not present | Optional check against real (non-public) AFM data of the developers; not available in a clean clone. |
| `integration/test_imports_api.py::test_real_tswat_export_import_when_available` | developer-local `annotations.xml` not present | Same; the XML importer is covered by synthetic CVAT XML tests. |
| `unit/test_cvat_xml.py::test_real_tswat_export_when_available` | developer-local `annotations.xml` not present | Same. |
| `model_contract/test_sam3_sam21_placeholder.py` (one case) | could not import `torch` | Requires the optional `sam` extra; the other placeholder tests (exact unavailable reasons, diagnostics exit codes, registry listing) passed. |

### 4.3 Warnings and observations

No test failed. The runs reported warnings that do not affect the results but are worth recording:

- `DeprecationWarning` from `pycocotools/mask.py` with NumPy 2.5 (`__array__` without the `copy` keyword) - nearly all of the 375 warnings in the CPU run and of the 360 warnings in the browser run. It originates in the third-party package when decoding RLE masks; a future NumPy release could turn it into an error, so the pinned versions in `requirements-lock.txt` remain relevant.
- `StarletteDeprecationWarning` (use of `httpx` with `starlette.testclient`) and one `PytestRemovedIn10Warning` from the pytest-playwright plugin - test-tooling deprecations only.
- `ruff format --check` reports three files (`src/afmlabel/exports/service.py`, `tests/browser/test_help_accessibility.py`, `tests/browser/test_workspace.py`) that the formatter would change. Formatting is not part of the project's check scripts (`scripts/dev/check.*` run `ruff check` only), so this is not a failed check; it is listed for completeness.
- mypy notes an unused `[[tool.mypy.overrides]]` entry for `huggingface_hub.*` and `torchvision.*` (these packages were not installed); the type check itself passed.
- The slowest tests are the browser test with 500 datasets (30.2 s) and, in the CPU suite, an export test writing PNG, TIFF and colour masks (2.2 s).

### 4.4 Not verified in this run

- Real SAM 3, SAM 3.1 and SAM 2.1 inference (`real_model` tests, `scripts/benchmark_sam31.py`) - requires the `sam` extra, gated checkpoints and, for SAM 3.1, CUDA. The backend code paths for unavailable models (exact reasons, exit codes, UI disabling) were verified.
- Firefox, Linux and macOS - only Chromium on Windows 11 was used here; the bootstrap script supports installing Firefox for Playwright.
- Slurm launch scripts and `scripts/download_models.py` - no automated tests exist; they need a cluster or network access.

## 5. Reproducing the runs

```powershell
git clone https://github.com/It4innovations/AFMLabel.git afmlabel; cd afmlabel
py -3.13 -m venv .venv; .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip; pip install -e ".[dev]"
python -m pytest -m "not browser and not real_model and not gpu"
python -m playwright install chromium
python -m pytest -m browser --browser chromium
python -m ruff check src tests scripts; python -m mypy src
python scripts/add_license_headers.py --check; python scripts/generate_help_reference.py --check
python -m afmlabel.diagnostics.privacy
```

Or run `.\scripts\dev\check.ps1 -Browser` (Windows) / `scripts/dev/check.sh` (Linux, macOS).