# AFMLabel - functional requirements analysis

**Document purpose:** states what AFMLabel must do and under which conditions, as implemented in the source code, and traces each requirement to the implementing modules and the automated tests that verify it.
**Software version:** AFMLabel 1.0.0

The requirements below were derived from the implemented behaviour (source code in `src/afmlabel/`, the user guide, the README and the test suite in `tests/`). Module paths are relative to `src/afmlabel/`; test paths are relative to `tests/`.

## 1. Purpose and scope

### 1.1 Purpose

Supervised segmentation models for atomic force microscopy (AFM) need pixel-accurate multiclass ground truth. AFM instruments record several co-registered channels of one field of view (topography/height, friction/lateral force, phase, amplitude, ...); material boundaries are often visible in only one of them. AFMLabel lets a domain expert annotate such a multi-channel field of view **once**, switching freely between channels, and produces exported masks with **exactly one class per pixel**, together with the information needed to trace every exported pixel back to its source images, annotation operations, model proposals and validation.

### 1.2 Scope

In scope:

- projects with a class ontology and 1 to 64 channel definitions;
- import of co-registered images, pairing of files into datasets;
- interactive annotation with independent, possibly overlapping regions (brush, eraser, polygon, fill, structural edits, undo/redo);
- optional local model proposals (SAM 3, SAM 3.1, SAM 2.1) and a deterministic fake backend for testing;
- import of legacy annotations (CVAT 1.1 XML exported by TSWAT; flattened label rasters);
- live Findings (overlaps, missing coverage and other problems), review and validation;
- export in the NanoUNet training layout and in a native exact layout;
- audit log, provenance, schema versions and migrations;
- single-account authentication, local administration and diagnostics.

Out of scope (not implemented): image registration or resampling of channels, AFM data preprocessing such as plane levelling (sources are only displayed with a percentile stretch), model training, multi-user accounts and roles, concurrent editing of one project by several sessions, and any runtime network service (the application never downloads or uploads data while running).

### 1.3 Definitions

| Term | Meaning |
| --- | --- |
| Channel | One source image slot of a dataset (key, label, filename tokens), defined per project. |
| Dataset | The source images (one per channel) of one field of view plus one shared annotation grid of identical width and height. |
| Region | An independent annotation object: one class, one binary mask, z-order, visibility, lock state, name and provenance. |
| Composition | The label map obtained by painting composable regions (visible, class enabled) in ascending z-order; the highest z-order wins. |
| Finding | A validation or review item computed live from the regions (overlap, missing coverage, hidden region, ...). It is never geometry. |
| Validation report | Stored result of *Validate* for one dataset revision, including the SHA-256 of the composition. |
| Export profile | A transformation of validated compositions into a file layout (NanoUNet or native exact). |

## 2. Actors and user roles

AFMLabel has one application account per installation; the roles below are roles of people and systems, not separate accounts.

| Actor | Description | Interaction |
| --- | --- | --- |
| Annotator (domain scientist) | Creates projects, imports images, annotates, reviews Findings, validates and exports. | Browser UI. |
| Administrator | Installs the software, edits `config/app.yaml`, creates or resets the account, enables and downloads optional model checkpoints, runs diagnostics, backs up the workspace. | CLI (`afmlabel`), scripts, configuration files. |
| Downstream consumer | Training pipeline (for example the NanoUNet loader) or analyst consuming the export packages. | Files in the exported ZIP (`manifest.json`, masks). |
| Reviewer / auditor | Checks how a mask was produced. | Audit log, manifests, region provenance, validation reports. |
| Local segmentation backend | Optional model (fake, sam3, sam31, sam21) that proposes masks on request. | In-process protocol, invoked by the job manager. |

## 3. Use cases

| ID | Use case | Primary actor | Main flow (summary) |
| --- | --- | --- | --- |
| UC-01 | Sign in / sign out | Annotator | Open the URL, enter username and password, work, sign out or let the session expire. |
| UC-02 | Create and configure a project | Annotator | Create project with name and channel count; adjust channel labels/tokens; add classes with colour, role and export id. |
| UC-03 | Import and pair images | Annotator | Choose files per channel or all at once, upload, inspect the pairing preview, commit the `OK` rows as datasets. |
| UC-04 | Annotate a dataset | Annotator | Open the workspace, choose an active class, draw with brush/polygon/fill, switch channels, edit regions (merge, split, z-order, subtract, delete), undo/redo. |
| UC-05 | Obtain a model proposal | Annotator | Select a backend, place point/box/text prompts (optionally refine a selected region), accept, add to selection or reject the proposal. |
| UC-06 | Import existing annotations | Annotator | Upload a CVAT 1.1 XML or a flattened label raster, map labels/values to classes, preview, commit. |
| UC-07 | Review Findings and validate | Annotator | Inspect Findings, resolve overlaps geometrically or mark them reviewed, fill missing pixels, run *Validate*. |
| UC-08 | Export ground truth | Annotator | Open the export dialog, choose profile and options, create the export, download the ZIP. |
| UC-09 | Replace a source image | Annotator | Upload a replacement for one channel, check the compatibility report, confirm. |
| UC-10 | Trace an exported mask | Reviewer | Read `manifest.json`, the audit log and region provenance; recompute the composition hash. |
| UC-11 | Administer the installation | Administrator | Configure, set password, revoke sessions, run health/privacy/model diagnostics, back up or restore the workspace, upgrade. |
| UC-12 | Enable an optional model backend | Administrator | Install the `sam` extra and PyTorch, download pinned checkpoints, enable in configuration, verify with the model diagnostic. |

## 4. Functional requirements

Each requirement is stated as implemented. "Test" lists the automated tests that exercise it (see section 7 for the full traceability table).

### 4.1 Projects and channels

- **FR-01 Project management.** The system shall create, list, search (by name), open, rename and delete projects. Deleting moves the project directory to `workspace/trash/` (recoverable) and is recorded in the audit log.
- **FR-02 Channel definitions.** A project shall define 1 to 64 channels, each with a unique label (max. 60 characters), a stable key `[a-z][a-z0-9_]*` and filename tokens. The first two default to `topography` and `friction` with their classic tokens; further channels default to `channel_<n>`. Channels shall be editable only while the project has no datasets; a channel key never changes once saved.
- **FR-03 Class ontology.** A project shall hold classes with numeric id (identity), stable key, name (unique, case-insensitive), display colour `#rrggbb`, semantic role (`MaterialLayer`, `Boundary`, `Unspecified`), enabled flag and optional export id (unique). At most one enabled `Boundary` class is allowed. A class used by regions cannot be deleted (`CLASS_IN_USE`), only disabled.
- **FR-04 Editing lock.** Only one browser tab shall edit a project at a time. The lock has a heartbeat, becomes stale after 90 s without heartbeat, can be taken over explicitly (recorded in the audit log), and other tabs can open the project read-only. Mutating endpoints require the lock.
- **FR-05 Resume state.** The system shall persist the per-project UI state (last dataset, displayed channel, tool, zoom/pan, brush radius, selected backend, fill options) and restore it when the page is reopened.

### 4.2 Import and pairing

- **FR-06 Batch upload.** The system shall accept TIFF, PNG and JPEG files (configurable extensions, default max. 512 MB per file, chunked streaming) per channel, or through an *All channels at once* picker that assigns each file to the channel whose filename token it contains; unmatched files are listed and skipped.
- **FR-07 Pairing preview.** Files shall be grouped into datasets by the pairing key (filename stem without channel tokens and separators). Each preview row has a status `OK`, `Missing channel`, `Ambiguous`, `Invalid file`, `Dimension mismatch` or `Calibration mismatch`; only `OK` rows can be committed. Pairing by sorted filename order is offered only when no key can be extracted and requires explicit confirmation. Nothing is written to the project before commit.
- **FR-08 Lossless decoding.** Source files shall be decoded without modification (Pillow, tifffile fallback for scientific TIFF variants), storing original filename, SHA-256, size, dimensions, dtype, bit depth, sample count, format, lossy-format flag and calibration read from TIFF resolution tags (never invented). Images are never resampled; all channels of a dataset must have identical dimensions and compatible calibration.
- **FR-09 Dataset creation and navigation.** Commit shall create one dataset per row atomically (staging directory, checksum verification after copy, rename). The project page shall list datasets with thumbnails and support case-insensitive search over original filenames, pairing key, name and id; the workspace *Previous/Next* follows the filtered list.
- **FR-10 Source replacement.** A channel image of a dataset may be replaced after a compatibility report (dimensions, calibration, checksum); annotations are kept, the dataset revision increases, the stored validation becomes stale and the replacement is recorded in the dataset and audit log.
- **FR-11 Deterministic display.** Non-8-bit channels shall be rendered for display by a deterministic percentile stretch (0.5th to 99.5th percentile to 8 bit); the same rendering feeds the canvas, thumbnails, SAM input and the NanoUNet channel PNGs, and its parameters are recorded in manifests.

### 4.3 Annotation tools

- **FR-12 Regions.** Annotations shall be stored as independent regions (class, COCO-RLE mask, unique z-order, visibility, lock, optional name, source, provenance, revision). Regions may overlap while working.
- **FR-13 Brush, eraser and polygon.** The brush shall follow the fixed rule: extend the selected editable region if its class equals the active class, otherwise create a new region of the active class (also over locked regions). The eraser removes pixels from the selected region only. Polygons create a new region (`Enter`), add to (`Shift+Enter`) or subtract from (`Alt+Enter`) the selected region; polygon pixel membership is defined by server-side rasterisation.
- **FR-14 Fill tool.** A click into an unlabelled area shall create a region of the active class covering the connected unlabelled pixels in label space, with configurable gap closing (0 to 20 px) and 4/8-connectivity, a live preview and a confirmation when the fill exceeds a quarter of the image. *Fill all enclosed gaps* creates one region per enclosed unlabelled component (optionally including components touching the border) in one transaction (max. 5 000 regions).
- **FR-15 Selection and structural edits.** The system shall support multi-selection (click, Ctrl+Click, Shift+Click, Ctrl+A), class/visibility/lock/name changes, merge (same class only, keeps primary id), split into connected components, duplicate, Delete Selected (locked regions kept), z-order reordering by drag, and *Subtract by Order* with a preview, refusal for locked regions that would lose pixels and explicit confirmation for regions that would become empty.
- **FR-16 Undo/redo.** Every edit shall be undoable and redoable; structural operations are undone by restoring a validated snapshot of the whole region set through the server so that ids, masks, classes, order, lock/visibility, provenance and review state are restored exactly.
- **FR-17 Channel switching.** All channels shall share one annotation; switching (buttons for two channels, navigator with slider/number/list for three or more, keys `1`-`9`/`0`, `Tab`/`Shift+Tab`) changes only the displayed image; regions, zoom, pan, selection and tool are kept.
- **FR-18 View-only adjustments.** Brightness and contrast sliders shall change only the canvas rendering of the source image; they are not persisted, not exported and not part of model input.
- **FR-19 Revisioned autosave.** Every edit shall be saved immediately as one API operation with an optimistic revision check (`REVISION_CONFLICT` on mismatch); each successful edit increases the dataset revision and invalidates the stored validation.

### 4.4 SAM proposals

- **FR-20 Backend registry.** The system shall always list the backends `fake`, `sam3`, `sam31` and `sam21` with availability, load state and an exact unavailable reason (disabled in configuration, checkpoint or dependency missing, no CUDA device, load failed). There is no silent fallback between backends. Real models are loaded only on explicit request, in a background thread.
- **FR-21 Prompt types and capability gating.** Supported prompt kinds are positive/negative points, box, mask (refine the selected region) and text, depending on the backend's advertised capabilities; unsupported prompts are rejected with `MODEL_CAPABILITY_UNSUPPORTED` and the corresponding UI controls are disabled with the reason.
- **FR-22 Inference jobs.** Inference shall run in a local job manager with a bounded queue (default 8), a configurable number of workers (default 1), cancellation, typed errors (including `MODEL_OOM`; two consecutive OOMs disable the backend for the process) and persisted job records; jobs interrupted by a restart are marked failed.
- **FR-23 Proposal acceptance and provenance.** A proposal shall not change the annotation until the user accepts it as a new region or adds it to the selected region. Accepted regions record backend id, model repository and revision, checkpoint SHA-256, code revision, device, prompts, displayed channel and source checksum; a model region edited later is flagged `user_corrected`.
- **FR-24 Model verification.** Model revisions shall be pinned to exact commits (moving references such as `main` are rejected); a local manifest with per-file SHA-256 is written by the download script and checked at runtime; `python -m afmlabel.diagnostics.models --backend <id>` performs a real inference on a synthetic image and exits `0` (usable), `2` (unavailable) or `1` (diagnostic failed).

### 4.5 Importing annotations

- **FR-25 CVAT/TSWAT XML import.** The system shall stage a *CVAT for images 1.1* XML, match XML images to datasets by the original filename of any channel (or an explicitly chosen target dataset), map each label to an existing class, a new class or "do not import", and on commit create one region per mask, polygon or box with XML provenance. Overlaps are preserved and start unreviewed; dimension mismatches are refused.
- **FR-26 Flattened mask import.** The system shall import a single-channel label raster with exactly the dataset's dimensions: every non-zero value is mapped to a class (default proposal: an enabled class keyed or named `buffer`, otherwise the project's default import class, otherwise the user must choose); discarding a value requires explicit confirmation; each connected component becomes a region (default) with source `lossy_import`, which validation keeps reporting as a warning. Colour label images are refused.

### 4.6 Findings and validation

- **FR-27 Live Findings.** The Findings list shall be computed from the current region set on every request: overlaps, missing coverage, hidden regions, class and Boundary policy problems, source problems, lossy imports and corrected model proposals, each with severity (blocking/warning). Default order: unreviewed blocking, unreviewed warnings, reviewed; filters by review state, severity and type; multi-selection.
- **FR-28 Overlap review.** Overlapping pixels shall be grouped into 4-connected overlap groups whose id is a digest of the participating region ids and the pixel set. Reviewing (resolved by order, intentional) changes review state only and never geometry; a review is dropped as soon as the overlapping pixels change; reordering alone keeps it.
- **FR-29 Validation.** *Validate* shall compose the final mask and block on: unreviewed overlaps (when review is required), unlabelled pixels, hidden regions, missing or disabled classes, duplicate ids, non-unique export ids, blocked or dimension-mismatched sources, Boundary policy violations and violation of the exactly-one-valid-class invariant. The report is stored with the dataset revision and the SHA-256 of the composition; any later edit makes it stale.
- **FR-30 Boundary policy.** The Boundary policy (`disabled`, `explicit_class`, `fill_unlabeled_after_approval`; global with per-project override) shall decide how Boundary-class regions and unlabelled pixels are treated. In `fill_unlabeled_after_approval` mode a *Boundary fill* with a preview of the exact pixel count creates one Boundary region covering only unlabelled pixels at the bottom of the z-order; it never overwrites a label and is undoable.

### 4.7 Export profiles

- **FR-31 Validation gate.** A dataset shall be exported only if its stored report is `valid`, was computed for the current dataset revision, the recomputed composition hash equals the stored one, and no pixel is unlabelled; otherwise it is listed as skipped with a code (`VALIDATION_MISSING`, `VALIDATION_STALE`, `VALIDATION_FAILED`, `UNLABELED_PIXELS`). Export never changes the annotation.
- **FR-32 NanoUNet profile.** The default profile shall write one folder per channel (`Topography/`, `Friction/`, further channels named after their label) with 8-bit grayscale PNG renders, `Masks_instance/<stem>.tif` (or `.png`) with one `uint16` value per pixel, optional `Masks_color/<stem>.png`, `manifest.json` and `classes.json`. Sample stems derive from the pairing key; duplicates get `~2`, `~3`; if stems collide under the loader's first-digit-run matching, all stems get a zero-padded sequence prefix.
- **FR-33 Native exact profile.** The native profile shall write per dataset `final_mask`, `instance_mask`, `mask_color.png`, untouched copies of every source channel, `regions.json`, `validation.json` and `dataset.json` with the SHA-256 of every written file.
- **FR-34 Export options and mask semantics.** Options: mask values = class export id or unique instance id per winning region (z-order); Boundary pixels exported as class value or `0`; TIFF (uncompressed) or PNG; colour visualisation on/off. Masks are written losslessly as `uint16`; the colour mask uses the exact class colours from the same composition; the manifest lists classes, export values, colour legend, normalisation parameters and per-dataset provenance.
- **FR-35 Packaging and history.** Each export shall be packaged as a ZIP whose SHA-256 and size are recorded in `export.json` and in the project's export history; packages can be listed, downloaded (authenticated, project-scoped) and deleted.

### 4.8 Audit and traceability

- **FR-36 Audit log.** Every state change (project, class, channel, dataset, region, review, validation, import, export, lock takeover) shall append an event with id, timestamp, operation, dataset, region and class ids, previous/new revision, source, actor, provenance and details to `logs/events.jsonl`; the project page shows the log.
- **FR-37 Provenance.** Regions shall carry provenance of their origin (tool, channel, seed and fill options; model backend data; XML or raster importer data with file checksum, mapping and importer version; merge/split/subtract history). Export manifests record per dataset the pairing key, original filenames, source SHA-256 per channel, composition SHA-256, class histogram, overlap review summary and provenance summary.

### 4.9 Authentication and security

- **FR-38 Single account.** One application account shall be created or changed with `afmlabel auth set-password` (prompt or `AFMLABEL_PASSWORD`), stored as an Argon2id hash only; passwords need at least 8 characters without leading/trailing whitespace; changing the password revokes all sessions. Until an account exists, pages redirect to `/setup`.
- **FR-39 Sessions.** Login shall issue an opaque random token in an `HttpOnly`, `SameSite=Lax` cookie (optionally `Secure`); the server stores only its SHA-256, expires sessions after inactivity (default 120 min), keeps sessions across restarts and allows revocation (`afmlabel auth revoke-sessions`).
- **FR-40 Login rate limiting.** Failed logins shall be limited per client (default 10 per 300 s, `AUTH_RATE_LIMITED`); response timing does not reveal whether the username exists.
- **FR-41 Access control and CSRF.** All paths except an explicit public list shall require a valid session (API: `401 AUTH_REQUIRED`; pages: redirect to login). State-changing API requests require the header `X-Requested-With: AFMLabel` and a same-origin `Origin`/`Sec-Fetch-Site`/`Referer` (`403 CSRF_REJECTED`).
- **FR-42 Input hardening.** Identifiers shall be validated against strict patterns, path traversal attempts rejected, uploaded filenames sanitised, and export downloads limited to the export's project.
- **FR-43 Secret protection.** Secrets (password, session tokens, `HF_TOKEN`, Authorization/Cookie headers) shall be redacted from logs and error payloads; secret-like keys in configuration files are rejected at start-up.

### 4.10 Administration

- **FR-44 Configuration.** Static configuration shall be read from `config/app.yaml` (optional; safe defaults otherwise), with environment overrides `AFMLABEL_<SECTION>__<KEY>`, a git-ignored `developer.local.yaml` overlay, and rejection of unknown keys.
- **FR-45 Health and resources.** The system shall expose `/api/v1/health` (Python, filesystem, RAM, disk, GPU, PyTorch presence), `/api/v1/version`, `/api/v1/system/resources` (CPU, RAM, disk, process RSS, GPU via `nvidia-smi`, low-RAM/low-disk warnings) and `/api/v1/models`; a failing metric is reported as `RESOURCE_METRIC_UNAVAILABLE` without breaking the editor. The UI shows a status bar and a System status dialog.
- **FR-46 Diagnostics CLIs.** `python -m afmlabel.diagnostics.system|privacy|models` shall report system health, external endpoints/telemetry settings/remote asset references, and model usability.
- **FR-47 Schema versions and migrations.** Every persisted document shall carry `schema_version` and be migrated on read by registered step functions; a document newer than the running build is refused, never downgraded.
- **FR-48 Crash safety and recovery.** Authoritative files shall be written atomically; leftover temporary files are removed at start-up; a failed mutation leaves no partial state; torn audit-log lines and corrupt lock files are tolerated.
- **FR-49 Help.** Every control shall have contextual help from a central registry (tooltip and extended text); `/help` shall render the user guide offline, including its screenshots; the keyboard-shortcut table in the guide is generated from the shortcut registry.
- **FR-50 Helper tools.** The repository shall provide a demo project generator (`scripts/create_demo_project.py`), Slurm launch scripts for interactive and batch use on a cluster, a model download script and a SAM benchmark script.

## 5. Non-functional requirements

- **NFR-01 Offline operation.** The application shall make no outbound network connection at runtime (no CDN assets, analytics, telemetry or model downloads); model acquisition is a separate administrator script. The default bind address is `127.0.0.1`; remote use goes through an SSH tunnel or a TLS reverse proxy.
- **NFR-02 Platforms.** Python 3.12 or 3.13 (3.12 is required by the pinned SAM 3 code) on Windows, Linux and macOS; Chrome or Firefox; no Qt or other desktop GUI dependency. Everything except the optional `sam31` backend works without a GPU.
- **NFR-03 Performance and resources.** Real models are never loaded at start-up; at most the configured number of inference jobs run concurrently with a bounded queue; channel images are loaded on demand and cached; resource snapshots are cached for the refresh interval; projects with hundreds of datasets remain navigable (browser test with 500 datasets). Documented model memory: `sam21` about 1.4 GiB RAM, `sam3` about 7.7 GiB RAM on CPU.
- **NFR-04 Data integrity.** Source files are immutable; masks are stored losslessly (COCO RLE) and exported pixel-exactly (`uint16`, values never rescaled); composition is deterministic and hashed; an exported mask has exactly one value per pixel and no unreviewed blocking overlap; nothing is resampled.
- **NFR-05 Security.** Argon2id password hashing, server-side sessions, CSRF protection, optional secure cookies, secret redaction, path validation, no secrets in configuration files (see FR-38 to FR-43).
- **NFR-06 Licensing.** All source files carry an MIT SPDX header (checked by a test and a script); third-party model code and checkpoints are not part of the repository and keep their own licences.
- **NFR-07 Reproducibility.** Model checkpoints and code are pinned to exact revisions with checksums; export manifests record the AFMLabel version, options and normalisation parameters; dependencies can be installed from `requirements-lock.txt`.
- **NFR-08 Usability and accessibility.** Controls have accessible names and tooltips, the UI provides keyboard shortcuts, a light and a dark theme, and an offline user guide.
- **NFR-09 Maintainability.** Typed code checked by mypy (`disallow_untyped_defs`), linted by ruff, covered by unit, integration, model-contract and browser tests; one source of truth for the version (`src/afmlabel/__init__.py`).
- **NFR-10 Robustness on Windows.** File and directory replacement retries transient `PermissionError`s caused by readers, indexers or virus scanners.

## 6. Constraints and assumptions

- One application account per installation; there are no user roles. Several people can share the account, but only one browser tab edits a given project at a time (FR-04); the lock is a courtesy mechanism and optimistic revisions are the actual conflict guard.
- The workspace is a local or network filesystem accessed by a single AFMLabel process; there is no database server.
- The server speaks plain HTTP; confidentiality over a network requires an SSH tunnel or a TLS reverse proxy (`security.secure_cookies: true` behind HTTPS).
- The channels of a dataset are assumed to be co-registered by the instrument; AFMLabel checks equal dimensions and compatible calibration but performs no registration.
- Exported mask values must fit into `uint16` (0 to 65 535); class and export ids are chosen accordingly.
- The `fake` backend produces synthetic geometry for development and tests only; it must not be used for scientific annotation.
- SAM 3 and SAM 3.1 checkpoints are gated by Meta on Hugging Face and require accepting their licence and an `HF_TOKEN` for download; `sam31` requires CUDA.
- Real-model tests (`real_model`, `gpu` markers) require downloaded checkpoints and suitable hardware and are not part of the CPU-only test suite.

## 7. Traceability matrix

Modules are relative to `src/afmlabel/`, tests relative to `tests/`. `browser/` tests run with Playwright (Chromium); `model_contract/*_real.py` tests need real checkpoints and were not executed for this package (see [Verification](verification.md)).

| Req. | Implementing module(s) | Verifying test(s) |
| --- | --- | --- |
| FR-01 | `projects/store.py`, `api/projects.py`, `static/js/projects.js` | `unit/test_project_store.py::test_create_list_get_delete`; `integration/test_projects_api.py::test_project_crud_and_events`; `browser/test_smoke.py::test_create_project_manage_classes_and_reopen` |
| FR-02 | `domain/project.py` (`ChannelDefinition`, `validate_channels`), `api/projects.py`, `static/js/project.js` | `integration/test_channels_api.py` (3 tests); `unit/test_pairing.py::test_preview_with_three_named_channels` |
| FR-03 | `domain/project.py` (`ClassOntology`), `api/projects.py` (classes) | `integration/test_projects_api.py::test_class_ontology_endpoints`; `browser/test_smoke.py::test_create_project_manage_classes_and_reopen` |
| FR-04 | `persistence/locks.py`, `api/projects.py` (lock), `static/js/lock.js` | `unit/test_locks.py` (5 tests); `integration/test_projects_api.py::test_project_lock_lifecycle`; `integration/test_regions_api.py::test_regions_survive_restart_and_lock_enforced` |
| FR-05 | `domain/project.py` (`UiState`), `api/projects.py` (ui-state), `static/js/workspace.js` | `browser/test_workspace.py::test_brush_polygon_channel_and_persistence`; `browser/test_channels_and_fill.py::test_three_channel_navigator`; `unit/test_migrations_compat.py::test_v1_ui_state_modality_field_is_read_as_channel` |
| FR-06 | `imports/batches.py`, `api/datasets.py`, `static/js/datasets.js` | `integration/test_datasets_api.py::test_invalid_upload_types_and_size_limits`; `browser/test_import.py::test_batch_import_with_pairing_preview` |
| FR-07 | `imports/pairing.py`, `imports/batches.py` | `unit/test_pairing.py` (13 tests); `integration/test_datasets_api.py::test_ambiguous_and_mismatched_pairs_never_commit`; `browser/test_import.py` |
| FR-08 | `images/decode.py` | `unit/test_images.py` (decode, calibration tests) |
| FR-09 | `projects/datasets.py` (`commit_batch`), `api/datasets.py`, `static/js/dataset-filter.js` | `integration/test_datasets_api.py::test_batch_upload_preview_commit_and_navigation`; `integration/test_upgrade_api.py::test_dataset_search_query_matches_metadata`; `browser/test_search_import_export.py::test_dataset_search_with_500_datasets_and_filtered_navigation`; `browser/test_dataset_card_layout.py` |
| FR-10 | `projects/datasets.py` (`replacement_report`, `apply_replacement`), `api/datasets.py` | `integration/test_datasets_api.py::test_source_replacement_flow` |
| FR-11 | `images/display.py` | `unit/test_images.py::test_display_normalisation_is_deterministic_and_bounded`, `::test_display_handles_constant_and_nan_inputs`, `::test_rgb_uint8_passes_through` |
| FR-12 | `domain/region.py`, `annotations/service.py`, `api/regions.py` | `integration/test_regions_api.py::test_region_lifecycle` |
| FR-13 | `static/js/annotation-canvas.js`, `static/js/workspace.js`, `persistence/rle.py` (`polygon_to_mask`) | `browser/test_workspace.py`; `browser/test_regions_editing.py::test_brush_creates_class_b_region_over_class_a_and_reports_overlap`, `::test_locked_region_visuals_brush_over_and_eraser_refused`; `unit/test_rle.py::test_polygon_rasterisation` |
| FR-14 | `static/js/masks.js` (`fillFromSeed`, `freeComponents`), `api/regions.py` (bulk-create) | `browser/test_channels_and_fill.py::test_fill_tool_fills_enclosed_gap_and_fill_all`; `integration/test_fill_api.py::test_bulk_create_is_atomic_and_audited` |
| FR-15 | `annotations/service.py`, `api/regions.py`, `static/js/regions-panel.js` | `unit/test_region_operations.py` (10 tests); `integration/test_upgrade_api.py::test_subtract_by_order_*`, `::test_bulk_delete_and_restore_round_trip`; `browser/test_regions_editing.py` (5 tests); `browser/test_overlaps.py::test_overlaps_findings_and_zorder_drag` |
| FR-16 | `annotations/service.py` (`restore_snapshot`), `api/regions.py` (restore), `static/js/workspace.js` | `unit/test_region_operations.py::test_restore_snapshot_*`; `integration/test_upgrade_api.py::test_merge_then_restore_returns_original_ids`; `browser/test_regions_editing.py::test_merge_and_split_are_undoable_with_original_ids` |
| FR-17 | `static/js/workspace.js`, `templates/workspace.html`, `api/datasets.py` (image endpoint) | `browser/test_channels_and_fill.py::test_three_channel_navigator`; `integration/test_channels_api.py::test_two_channel_project_renders_buttons_and_single_channel_no_switch` |
| FR-18 | `static/js/annotation-canvas.js` (view filter) | `browser/test_view_controls.py::test_brightness_contrast_are_view_only` |
| FR-19 | `projects/store.py` (`mutate_project`), `projects/datasets.py` (`mutate`), `annotations/service.py` (`mutate`) | `unit/test_project_store.py::test_mutation_bumps_revision_and_records_event`, `::test_revision_conflict_is_detected_and_nothing_is_written`; `integration/test_regions_api.py` |
| FR-20 | `segmentation/registry.py`, `api/health.py` (models, load) | `unit/test_registry_loading.py` (4 tests); `model_contract/test_sam3_sam21_placeholder.py::test_registry_lists_all_backends_with_reasons`; `integration/test_health_api.py::test_models_lists_fake_and_sam31_placeholder`, `::test_model_load_endpoint` |
| FR-21 | `segmentation/base.py`, `segmentation/fake.py`, `sam3.py`, `sam31.py`, `sam21.py`, `static/js/segmentation.js` | `model_contract/test_fake_backend.py` (with `backend_contract.py`); `browser/test_proposals.py::test_proposal_accept_and_reject` |
| FR-22 | `jobs/manager.py`, `api/segmentation.py` | `unit/test_job_manager.py` (6 tests) |
| FR-23 | `jobs/manager.py` (`_run` provenance), `api/regions.py` (`sam_proposal` check), `annotations/service.py` | `integration/test_segmentation_api.py` (2 tests); `browser/test_proposals.py` |
| FR-24 | `segmentation/manifest.py`, `settings.py` (revision validator), `diagnostics/models.py`, `scripts/download_models.py` | `unit/test_manifest.py`; `unit/test_settings.py::test_moving_model_revisions_are_rejected`; `model_contract/test_sam3_sam21_placeholder.py`, `test_sam31_placeholder.py`; real-model tests (not run) |
| FR-25 | `imports/cvat_xml.py`, `imports/tswat.py`, `api/imports.py`, `static/js/legacy-import.js` | `unit/test_cvat_xml.py`; `integration/test_imports_api.py::test_hidden_region_regression_import`, `::test_dimension_mismatch_and_unmatched_images_are_not_imported`; `browser/test_legacy_import.py` |
| FR-26 | `imports/lossy.py`, `domain/project.py` (`find_default_import_class`), `api/imports.py` | `integration/test_imports_api.py::test_lossy_raster_import_is_marked_and_warned`; `integration/test_upgrade_api.py::test_flattened_import_rules`; `unit/test_migrations_compat.py::test_default_import_class_prefers_buffer_then_configured_key`; `browser/test_search_import_export.py::test_flattened_mask_import_defaults_to_buffer_and_selects_result` |
| FR-27 | `validation/findings.py`, `api/validation.py`, `static/js/findings.js` | `integration/test_upgrade_api.py::test_findings_are_live_sorted_and_bulk_reviewable`; `browser/test_findings_panel.py` |
| FR-28 | `overlaps/engine.py`, `validation/service.py` | `unit/test_overlaps.py::test_group_id_changes_when_coverage_changes_and_reviews_are_pruned` and 3 further overlap tests; `integration/test_validation_api.py::test_overlap_review_and_validation_flow`; `browser/test_overlaps.py` |
| FR-29 | `validation/engine.py`, `validation/service.py` | `unit/test_overlaps.py::test_validation_blocks_and_warns_correctly`, `::test_composition_is_deterministic`; `unit/test_region_operations.py::test_composition_has_exactly_one_winner_per_pixel_after_overlaps`; `integration/test_validation_api.py` |
| FR-30 | `validation/engine.py` (`boundary_fill_mask`), `validation/service.py`, `settings.py` | `unit/test_overlaps.py::test_boundary_policy_and_fill_never_overwrites_labels`; `integration/test_validation_api.py::test_boundary_fill_flow`; `unit/test_settings.py::test_invalid_boundary_mode_is_rejected` |
| FR-31 | `exports/service.py` (`_exact`) | `integration/test_exports_api.py::test_export_is_blocked_until_validated_then_packages`; `browser/test_exports.py` |
| FR-32 | `exports/nanounet.py`, `exports/rasters.py` (`export_stems`) | `integration/test_exports_api.py::test_nanounet_export_passes_the_downstream_loader_contract` (with `fixtures/nanounet_loader.py`); `unit/test_export_rasters.py::test_export_stems_are_deterministic_and_loader_safe`; `integration/test_upgrade_api.py::test_export_default_profile_naming_png_tiff_and_color_mask` |
| FR-33 | `exports/native_exact.py` | `integration/test_exports_api.py::test_export_is_blocked_until_validated_then_packages` (native profile) |
| FR-34 | `exports/base.py`, `exports/rasters.py`, `exports/service.py` | `unit/test_export_rasters.py` (6 tests); `integration/test_upgrade_api.py::test_export_default_profile_naming_png_tiff_and_color_mask`; `browser/test_search_import_export.py::test_export_dialog_defaults_search_naming_and_color_mask` |
| FR-35 | `exports/service.py`, `api/exports.py`, `static/js/exports.js` | `browser/test_exports.py::test_validate_and_export_download`; `integration/test_security.py::test_downloads_require_login_and_project_scope` |
| FR-36 | `persistence/jsonl.py`, `projects/store.py` (`record_event`), `api/projects.py` (events), `domain/events.py` | `unit/test_project_store.py::test_mutation_bumps_revision_and_records_event`; `integration/test_projects_api.py::test_project_crud_and_events`; `integration/test_fill_api.py::test_bulk_create_is_atomic_and_audited` |
| FR-37 | `annotations/service.py`, `jobs/manager.py`, `imports/tswat.py`, `imports/lossy.py`, `exports/native_exact.py` (`entry_for`) | `integration/test_segmentation_api.py`; `integration/test_imports_api.py`; `integration/test_exports_api.py` |
| FR-38 | `auth/account.py`, `auth/passwords.py`, `main.py` (`auth` CLI), `api/pages.py` (setup) | `unit/test_auth.py::test_password_hash_is_argon2id_and_verifies`, `::test_weak_passwords_are_rejected`, `::test_account_store_never_persists_plaintext`; `integration/test_auth_api.py::test_no_account_configured_redirects_to_setup` |
| FR-39 | `auth/sessions.py`, `api/auth.py` | `unit/test_auth.py::test_sessions_persist_and_expire`, `::test_session_revocation`; `integration/test_auth_api.py::test_login_sets_httponly_cookie_and_logout_clears_it`, `::test_session_survives_process_restart` |
| FR-40 | `auth/sessions.py` (`LoginRateLimiter`), `auth/passwords.py` (`DUMMY_HASH`) | `unit/test_auth.py::test_rate_limiter_blocks_after_limit`; `integration/test_auth_api.py::test_invalid_credentials_and_rate_limit` |
| FR-41 | `auth/middleware.py`, `static/js/api.js` | `integration/test_auth_api.py::test_unauthenticated_api_is_rejected_and_pages_redirect`, `::test_csrf_header_is_required_for_state_changes` |
| FR-42 | `domain/common.py` (`is_valid_id`), `projects/sanitize.py`, `exports/service.py` (`_record_path`), `templating.py` | `integration/test_security.py::test_path_traversal_attempts_are_rejected` (7 cases), `::test_downloads_require_login_and_project_scope`; `unit/test_project_store.py::test_path_traversal_ids_are_rejected` |
| FR-43 | `privacy/redaction.py`, `logging_config.py`, `settings.py` | `unit/test_redaction.py` (9 tests); `unit/test_settings.py::test_secret_keys_in_yaml_are_rejected`; `integration/test_security.py::test_secrets_never_appear_in_error_responses_or_logs`; `integration/test_auth_api.py::test_password_never_appears_in_logs` |
| FR-44 | `settings.py` | `unit/test_settings.py` (11 tests) |
| FR-45 | `api/health.py`, `diagnostics/system.py`, `resources/monitor.py`, `static/js/status-bar.js`, `system-status.js` | `integration/test_health_api.py` (7 tests); `unit/test_resources.py` (7 tests); `integration/test_security.py::test_resource_metric_failure_does_not_break_editor`, `::test_low_ram_and_disk_warnings_are_reported`; `browser/test_smoke.py::test_login_logout_and_system_status` |
| FR-46 | `diagnostics/system.py`, `diagnostics/privacy.py`, `diagnostics/models.py` | `model_contract/test_sam31_placeholder.py::test_diagnostic_cli`; `model_contract/test_fake_backend.py::test_fake_backend_passes_the_model_diagnostic`; privacy diagnostic run (see Verification) |
| FR-47 | `persistence/migrations.py`, `domain/common.py` | `unit/test_migrations_compat.py` (6 tests, fixtures `fixtures/schema_v1/`); `unit/test_project_store.py::test_schema_migration_interface` |
| FR-48 | `persistence/atomic.py`, `persistence/jsonl.py`, `persistence/locks.py`, `projects/store.py` (`recover_incomplete_writes`) | `unit/test_atomic.py` (7 tests); `unit/test_project_store.py::test_recover_incomplete_writes_removes_temp_files`, `::test_failed_mutation_does_not_persist_partial_state`; `unit/test_locks.py::test_corrupt_lock_file_is_treated_as_stale`; `unit/test_job_manager.py::test_jobs_are_persisted_and_interrupted_ones_recovered` |
| FR-49 | `help/registry.py`, `help/markdown.py`, `api/help.py`, `static/js/help.js`, `static/help/*.json`, `scripts/generate_help_reference.py` | `unit/test_help.py` (12 tests); `browser/test_help_accessibility.py` (3 tests); `integration/test_upgrade_api.py::test_help_pages_and_registry_are_served_locally` |
| FR-50 | `scripts/create_demo_project.py`, `scripts/slurm/*.sh`, `scripts/download_models.py`, `scripts/benchmark_sam31.py` | `integration/test_demo_project_script.py`; Slurm, download and benchmark scripts have no automated test |
| NFR-01 | `diagnostics/privacy.py`, `templating.py`, templates (local assets) | `integration/test_no_network_on_startup.py`; `integration/test_health_api.py::test_index_page_and_static_assets_are_local`; privacy diagnostic run |
| NFR-02 | `pyproject.toml` | `unit/test_no_qt.py` (3 tests); executed on Windows 11 / Python 3.13 / Chromium only (see Verification) |
| NFR-04 | `persistence/rle.py`, `overlaps/engine.py`, `exports/rasters.py` | `unit/test_rle.py`; `unit/test_export_rasters.py::test_label_raster_round_trip_is_pixel_exact`; `unit/test_overlaps.py::test_composition_is_deterministic` |
| NFR-06 | `scripts/add_license_headers.py` | `unit/test_license_headers.py` (3 tests) |
| NFR-08 | `static/help/registry.json`, `static/js/help.js`, `static/js/theme.js` | `browser/test_help_accessibility.py`; `browser/test_smoke.py::test_theme_toggle_persists` |
| NFR-10 | `persistence/atomic.py` (`replace_with_retry`) | `unit/test_atomic.py::test_replace_with_retry_survives_transient_permission_errors` |
