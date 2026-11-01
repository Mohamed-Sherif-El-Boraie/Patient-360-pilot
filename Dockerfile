# Build stage: uv installs the locked dependencies, then the project itself as a real (non-editable) package.
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app

# Dependencies first: this layer is cached until pyproject.toml or uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the project. Hatch packs everything under src/patient360 (the prompt .md and .json files too).
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# Shared run stage: the virtual environment, the SQL migrations, the catalogue workbook and the entrypoint script
# (files are root-owned and world-readable, which uid 10001 can read).
FROM python:3.13-slim AS base
RUN useradd --system --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY migrations ./migrations
COPY catalogue ./catalogue
COPY docker-entrypoint.sh docker-entrypoint-ssh.sh ./
# Only the connection checks, to run inside the App Service (SSH): python tools/check_connections.py
COPY tools ./tools
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 MIGRATIONS_DIR=/app/migrations \
    CATALOGUE_WORKBOOK=/app/catalogue/Packages_tests_paramters.xlsx
EXPOSE 8000

# Troubleshooting image, built only with `--target debug` and tagged apart from production (for example :v1-ssh).
# Adds the App Service SSH server (port 2222, reached only through the portal tunnel) and runs as root, which sshd
# needs. The root password is the fixed value App Service documents for this tunnel. Never deploy this to production.
FROM base AS debug
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && echo "root:Docker!" | chpasswd \
    && mkdir -p /run/sshd \
    && printf '%s\n' 'Port 2222' 'ListenAddress 0.0.0.0' 'LoginGraceTime 180' 'X11Forwarding no' \
        'Ciphers aes128-cbc,3des-cbc,aes256-cbc,aes128-ctr,aes192-ctr,aes256-ctr' \
        'MACs hmac-sha1,hmac-sha1-96' 'StrictModes yes' 'SyslogFacility DAEMON' 'PasswordAuthentication yes' \
        'PermitEmptyPasswords no' 'PermitRootLogin yes' 'Subsystem sftp internal-sftp' > /etc/ssh/sshd_config
ENTRYPOINT ["sh", "/app/docker-entrypoint-ssh.sh"]
CMD ["uvicorn", "--factory", "patient360.main:create_app_from_env", "--host", "0.0.0.0", "--port", "8000"]

# Production image: the last stage, so a plain `docker build` produces it. Runs as a non-root user.
FROM base AS prod
USER app
# Migrates and loads the catalogue (unless RUN_STARTUP_JOBS=0), then runs CMD. The sh form needs no exec bit.
ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=4)"]
CMD ["uvicorn", "--factory", "patient360.main:create_app_from_env", "--host", "0.0.0.0", "--port", "8000"]
