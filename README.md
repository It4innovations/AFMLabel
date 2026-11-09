# AFMLabel

AFMLabel is a browser-controlled, FastAPI-backed tool that creates
scientifically traceable multiclass segmentation ground truth from
co-registered atomic force microscopy (AFM) images. A dataset is a set of
1 to 64 source channels of one field of view (classically topography +
friction, optionally phase, amplitude and more) that share **one**
annotation; the exported mask has exactly one class per pixel.

- Browser UI (Chrome/Firefox), no CDN assets, no network access at runtime.
- Regions may overlap while working; Findings make overlaps and missing
  pixels visible, validation gates export.
- Brush, polygon, eraser, seed-fill of enclosed gaps, optional local SAM
  proposals (SAM 3 / 3.1 / 2.1), XML and flattened-mask import, NanoUNet
  and native exact export.
- Filesystem projects, atomic writes, append-only audit log, schema
  migrations.

**How to use the application** is described in
[`docs/user-guide.md`](docs/user-guide.md); the same page is served inside
the application under **Help**. This file covers installation and
starting the server.

## Prerequisites

- Python **3.12 or 3.13** (3.12 is the floor required by the pinned SAM 3
  code; the health endpoint reports the interpreter status).
- About 1 GB of disk for the application and its dependencies; the AFM
  images and annotations live in the configured workspace directory.
- Optional model support needs a supported GPU and the matching PyTorch
  build, or a CPU with enough RAM (`sam21` about 1.4 GiB, `sam3` about
  7.7 GiB). Nothing else needs a GPU.

Always install into a virtual environment, never into the system Python.

## Install on Windows (PowerShell)

```powershell
git clone <repository-url> afmlabel
cd afmlabel
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

`.\scripts\dev\bootstrap.ps1` runs the same steps. If PowerShell refuses to
run the activation script, start it once with
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

## Install on Linux

```bash
git clone <repository-url> afmlabel
cd afmlabel
python3.12 -m venv .venv            # or python3.13
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-lock.txt   # exact tested versions (optional)
pip install -e ".[dev]"
```

`scripts/dev/bootstrap.sh` runs the same steps. On a cluster without
Internet access on the compute nodes, install and download models on a
login or data-transfer node; the application itself never downloads
anything at runtime.

## Install on macOS

```bash
brew install python@3.13              # or use python.org / pyenv
git clone <repository-url> afmlabel
cd afmlabel
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

The application runs CPU-only on macOS (Intel and Apple silicon). The
optional `sam21` backend works on CPU; `sam3` needs about 7.7 GiB RAM with
`allow_cpu: true`; `sam31` is CUDA-only and therefore unavailable on macOS.

## Configure

```bash
cp config/app.example.yaml config/app.yaml     # PowerShell: Copy-Item
afmlabel auth set-password --username lab      # creates the single account
```

Every value in `config/app.yaml` can be overridden with an
`AFMLABEL_<SECTION>__<KEY>` environment variable. The application also
starts with safe built-in defaults when the file is absent (bind to
localhost, all model backends disabled). Important keys:

| Key | Purpose |
| --- | --- |
| `workspace.root` | Directory holding all projects, the account and the trash. |
| `server.host` / `server.port` | Bind address and port (default `127.0.0.1:9080`). Keep localhost and use an SSH tunnel or a TLS reverse proxy for remote access. |
| `annotation.boundary_mode` | `disabled`, `explicit_class` or `fill_unlabeled_after_approval`. |
| `annotation.require_overlap_review` | Unreviewed overlaps block export when `true`. |
| `export.default_profile` | `nanounet` (default) or `native_exact`. |
| `models.sam3` / `sam31` / `sam21` | Optional local SAM backends. |

Secrets (tokens, passwords) are never read from the YAML file.

## Run

```bash
afmlabel serve --host 127.0.0.1 --port 9080
```

Then open <http://127.0.0.1:9080>, sign in and follow the
[user guide](docs/user-guide.md). Useful endpoints: `/` (UI),
`/help` (the guide), `/api/v1/health`, `/api/v1/version`,
`/api/v1/system/resources`, `/api/v1/models`.

Remote access over SSH:

```bash
ssh -N -L 9080:127.0.0.1:9080 user@workstation     # then open http://127.0.0.1:9080
```

### Optional: local SAM backends

Install the PyTorch build matching your device first, then the extra and
the checkpoints (login node with Internet access on clusters):

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu   # or a CUDA index
pip install -e ".[sam]"
# sam3 / sam31 only: pinned official SAM 3 code
pip install --no-deps "git+https://github.com/facebookresearch/sam3.git@660a5e9e1b8b4c02c0ad97229b88a09a6e4ff5b7"
pip install "timm>=1.0.17" "ftfy==6.1.1" regex "iopath>=0.1.10" typing_extensions einops
export HF_TOKEN=hf_...                                  # gated repositories (sam3, sam31)
python scripts/download_models.py --model sam21         # no token needed
python scripts/download_models.py --model sam3
```

Enable the backend in `config/app.yaml` and verify it with
`python -m afmlabel.diagnostics.models --backend sam21` (exit code `0`
usable, `2` unavailable with the reason). Checkpoints are
licence-encumbered binaries and must not be redistributed.

## Checks and tests

```bash
ruff check src tests scripts
mypy src
pytest -m "not browser and not real_model and not gpu"   # CPU-only suite
playwright install chromium                               # once
pytest -m browser --browser chromium
python scripts/add_license_headers.py --check             # MIT headers
python scripts/generate_help_reference.py --check         # shortcut table in the guide
```

`scripts/dev/check.ps1` / `scripts/dev/check.sh` run the whole set.
Temporary test data lives under the git-ignored `tmp/` directory; never
run two pytest sessions of this repository at the same time (they share
`tmp/pytest`).

## Diagnostics

```bash
python -m afmlabel.diagnostics.system            # health report as JSON
python -m afmlabel.diagnostics.privacy           # external endpoints / telemetry scan
python -m afmlabel.diagnostics.models --backend fake
```

## Development

AI agent harnesses were used for selected development, testing, and
optimization tasks.

## License

MIT, Copyright (c) 2026 IT4Innovations, VSB-Technical University of
Ostrava (see [`LICENSE`](LICENSE)).

The IT4Innovations name and logo
(`src/afmlabel/static/img/it4i-logo.png`) are trademarks of IT4Innovations,
VSB-Technical University of Ostrava, and are not covered by the MIT licence.
Redistributions that are not published by IT4Innovations should remove or
replace the logo.

Third-party components are not part of this repository and keep their own
licences: the optional SAM 3 / SAM 3.1 code and checkpoints are distributed
by Meta under the SAM License (gated on Hugging Face), SAM 2.1 under
Apache-2.0. Python dependencies are installed from PyPI under their
respective licences.

## Acknowledgements

This work was supported by the Ministry of Education, Youth and Sports of
the Czech Republic through the e-INFRA CZ (ID:90254).
