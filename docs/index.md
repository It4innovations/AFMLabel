# AFMLabel - software documentation package

**Document purpose:** overview of the documentation package for the research result AFMLabel (type: software) and entry point to the individual documents.
**Software version:** AFMLabel 1.0.0 (`src/afmlabel/__init__.py`)

## About the software

AFMLabel is a browser-controlled application with a FastAPI (Python) backend that creates scientifically traceable multiclass segmentation ground truth from co-registered atomic force microscopy (AFM) images. A *dataset* is a set of 1 to 64 source channels of one field of view (classically topography and friction, optionally phase, amplitude and further channels) that share one annotation. Annotators draw independent, possibly overlapping regions with a brush, polygon, eraser and seed-fill tool, can request optional local SAM model proposals (Meta SAM 3, SAM 3.1, SAM 2.1), import legacy CVAT/TSWAT XML or flattened masks, and resolve overlaps and missing pixels through a live list of Findings. A validation step composes the final mask with exactly one class per pixel and stores a report with a hash of the composition; only datasets validated at their current revision can be exported, in the NanoUNet training layout or in a native exact layout with full provenance. All state lives in a filesystem workspace with atomic writes, schema versions with migrations and an append-only audit log. The application runs entirely on the local host and makes no network connections at runtime.

## Documents in this package

| Document | Content |
| --- | --- |
| [README](../README.md) | Installation on Windows, Linux and macOS, configuration, starting the server, optional model backends, checks (verbatim copy from the repository). |
| [User guide](user-guide.md) | Complete end-user workflow from sign-in to interpreting the exported mask, with screenshots; the same text is served offline under **Help** in the application. |
| [Functional requirements](functional-requirements.md) | Purpose and scope, actors, use cases, numbered functional (FR) and non-functional (NFR) requirements, constraints, and a traceability table requirement -> module -> test. |
| [Technical documentation](technical-documentation.md) | Architecture, components mapped to the package structure, data model and on-disk layout, schema versions and migrations, REST API, annotation and export algorithms, model backend interface, configuration, security design, design decisions. |
| [Programmer documentation](programmer-documentation.md) | Repository layout, development environment, build and install, coding conventions, checks, extension points, CLI reference and representative source code excerpts. |
| [Verification](verification.md) | Test strategy and suite structure, results of the test runs performed for this package, and static analysis results. |
| [images/](images/) | Architecture and data diagrams (SVG) and UI screenshots (PNG). |

## Download and usage

- **Source code:** <https://github.com/It4innovations/AFMLabel>
- **Licence:** MIT (copyright holder, trademark notice for the logo and acknowledgements in the [README](../README.md#license)). Optional third-party components keep their own licences (SAM 3 / SAM 3.1 code and checkpoints: Meta SAM License, gated; SAM 2.1: Apache-2.0); checkpoints are not redistributed with AFMLabel.
- **Requirements:** Python 3.12 or 3.13, about 1 GB of disk for the application and its dependencies, a current Chrome or Firefox. A GPU is needed only for the optional SAM 3.1 backend (SAM 3 and SAM 2.1 can also run on a CPU).
- **Install summary** (details in the [README](../README.md)):

  ```bash
  git clone https://github.com/It4innovations/AFMLabel.git afmlabel
  cd afmlabel
  python -m venv .venv            # Python 3.12 or 3.13; activate it
  pip install -e ".[dev]"
  cp config/app.example.yaml config/app.yaml
  afmlabel auth set-password --username lab
  afmlabel serve --host 127.0.0.1 --port 9080     # then open http://127.0.0.1:9080
  ```

- **Optional model support:** `pip install -e ".[sam]"` after the matching PyTorch build, `python scripts/download_models.py --model sam3|sam31|sam21`, then enable the backend in `config/app.yaml`.