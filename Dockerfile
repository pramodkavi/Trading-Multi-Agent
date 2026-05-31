# Multi-stage build for the crypto-signals scan runner (Slice 1 Step 1.13).
#
# Stage 1 (builder): resolve and install the package + its dependencies into
# an isolated virtualenv. This stage carries pip, build tooling, and wheel
# caches that we do NOT want in the shipped image.
#
# Stage 2 (runtime): copy only the populated venv plus the source and the
# operational scripts. No compilers, no pip cache -> smaller image, smaller
# attack surface, faster cold-pull on Fargate (one task per scheduled scan).
#
# The app is one-shot: the entrypoint runs a single scan and exits. In
# production EventBridge Scheduler launches one Fargate task per scan; the
# same one-shot behaviour is exercised locally via `docker compose up`.

# ---------------------------------------------------------------------------
# Stage 1: builder
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

# Faster, quieter, deterministic pip.
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build into a dedicated venv so it copies cleanly into the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Copy only what the install needs first. pyproject.toml pins the deps and
# references README.md via `readme = "README.md"`, so both must be present.
# src/ is the installable package (`include = ["src*"]`).
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Install the project (runtime deps only -- not the [dev] extra). This places
# the `src` package and every dependency into /opt/venv.
RUN pip install .

# ---------------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Non-root user: never run the container as root.
RUN useradd --create-home --uid 1000 appuser

# Bring over the fully-populated virtualenv from the builder.
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# The `src` package is already installed in the venv; we still need the
# operational scripts (not part of the installed package) to run the scan.
COPY scripts/ ./scripts/

USER appuser

# One-shot: run a single scan for the first configured symbol, then exit.
# Override the symbol with `docker run ... --symbol ETHUSDT` or compose command.
ENTRYPOINT ["python", "scripts/run_scan.py"]
