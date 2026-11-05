# Builds, compatibility and maintenance

## Checked environment

Publication preparation on 2026-10-29 used Linux x86_64, Python 3.14.4,
Docker Engine 29.1.3 and Docker Compose 2.40.3. The installed MCP SDK was 1.30.0,
httpx 0.28.1 and psutil 7.2.2. The official Python SDK client initialized the
Streamable HTTP endpoint and exercised tool calls. Raw HTTP contract checks also
use protocol version `2025-06-18`; that alone is not a client compatibility matrix.
GitHub Actions is configured but has not run on a remote repository.

Docker Desktop on macOS/Windows, ARM, rootless Docker, other Docker log drivers,
older Python/Compose releases, other MCP SDKs and desktop clients have not been
verified here. Bind permissions, socket paths, metrics visibility and log behavior
can differ. The integration script requires a local Linux Docker socket and the
`json-file` driver for its controlled rotation fixture. Newer versions may work
but are not covered by this record. Use the checked combination first.

The API, tools, response schemas, provider factory and state format are
experimental. No semantic-versioning/stable upgrade promise is made. The existing
version-2 baseline and legacy cursors have regression tests; this does not cover
all historical deployments. Unsupported old state without pending work can be
ignored with a warning; pending work fails rather than being reset. Back up the
baseline and snapshots together before upgrading. Filesystem locking uses Linux
`flock`; distributed filesystems and power-loss durability remain unverified.

## Pinned inputs

- `Dockerfile` and both Python fixtures pin `python:3.14-slim` by registry digest
  `sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8`.
  This was the locally available base, successfully used in the publication build.
- Both Compose files retain the existing Docker socket proxy digest.
- `requirements.txt` declares direct dependencies and acceptable ranges.
  `requirements-lock.txt` keeps the existing exact version constraints.
  `requirements-hashed.txt` records their resolved transitive closure and SHA-256
  hashes obtained from the public PyPI index. Docker, CI and documented installs
  use `--require-hashes --only-binary=:all:` with that file. A missing compatible
  wheel fails explicitly instead of building a source package.
- `.dockerignore` admits only runtime Python files and the hashed dependency file.
  Credentials, test outputs and local state never need to enter the build context.

Hashes check downloaded bytes against recorded values, not package trust or an
absence of vulnerabilities. The lock includes hashes for multiple distribution
files; the installer selects a compatible wheel. Only Linux amd64 / Python 3.14
was installed and tested. These controls reduce mutable inputs; they do not
guarantee byte-identical images (build tooling, platform and timestamps still
matter). The image's `useradd` also uses files from the pinned base filesystem.
No OS package update is performed during the build.

## Updating deliberately

Review base/proxy image releases and package/security updates before releases and
periodically during maintenance. Digest pinning also pins old vulnerabilities.
Select and record new digests using registry metadata, then update all affected
references. Do not silently replace a tag's meaning in the verification record.

After editing direct requirements and version constraints, regenerate hashes
using the reviewed constraints (the publication run used uv 0.13.5):

```sh
uv pip compile requirements.txt -c requirements-lock.txt --generate-hashes \
  --no-header --no-annotate --default-index https://pypi.org/simple \
  --python-version 3.14 -o requirements-hashed.txt
python3.14 -m venv /tmp/shadow-check-venv
/tmp/shadow-check-venv/bin/python -m pip install --require-hashes \
  --only-binary=:all: -r requirements-hashed.txt
/tmp/shadow-check-venv/bin/python -m pip check
/tmp/shadow-check-venv/bin/python -m unittest discover -s tests -v
bash scripts/run_integration.sh
```

If intentionally upgrading a constrained package, first update its constraint and
review the newly resolved closure. Compare versions in both lock files and run
checks from a clean virtual environment. uv is a maintenance tool, not a runtime
dependency. Record changed versions/platforms and failures. No vulnerability audit
or bit-for-bit reproducibility test was performed during this preparation.
