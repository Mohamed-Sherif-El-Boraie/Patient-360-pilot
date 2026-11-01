"""Checks, one by one, what the settings in the environment can really reach: the settings themselves, PostgreSQL, the
Blob containers (raw + the four output containers) and the Backend login. Prints PASS or FAIL for each, never a secret.
(S3 has its own script: tools/check_s3_upload.py.)

Run it with the image, the secrets coming from your .env file (from the project folder, in cmd):

    docker run --rm --env-file .env -v "%cd%\\tools:/app/tools:ro" --entrypoint python ^
        360acr.azurecr.io/patient360:v1 tools/check_connections.py

It writes one tiny file named ``_connectivity-test/<time>.txt`` into each Blob container (safe to delete; the Function
app ignores that path). The output containers only allow create + write, so those are checked by writing, not reading.
"""
import sys
from collections.abc import Callable
from datetime import UTC, datetime

import psycopg
from azure.core.exceptions import AzureError

from patient360.adapters.backend import WrapperBackend
from patient360.adapters.blob import build_output_store
from patient360.config.settings import Settings, SettingsError, load_settings

STAMP = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
PATH = f"_connectivity-test/{STAMP}.txt"
BODY = b"patient360 connectivity test, safe to delete\n"


def why(error: BaseException) -> str:
    """What to show for a failure. An Azure error shows only its type and code: its text may hold a URL with a token."""
    error = error.__cause__ if isinstance(error.__cause__, AzureError) else error
    if isinstance(error, AzureError):
        return f"{type(error).__name__} ({getattr(error, 'error_code', None) or getattr(error, 'status_code', None)})"
    return f"{type(error).__name__}: {(str(error).splitlines() or [''])[0][:150]}"


def check(name: str, run: Callable[[], str | None]) -> bool:
    try:
        detail = run()
    except Exception as error:
        print(f"FAIL  {name}: {why(error)}")
        return False
    print(f"PASS  {name}" + (f": {detail}" if detail else ""))
    return True


def postgres(s: Settings) -> str:
    with psycopg.connect(host=s.pg_host, port=s.pg_port, dbname=s.pg_database, user=s.pg_user, password=s.pg_password,
                         sslmode=s.pg_ssl_mode, connect_timeout=10) as conn:
        can_create = conn.execute("SELECT has_database_privilege(current_user, current_database(), 'CREATE')"
                                  ).fetchone()[0]
        schema = s.pg_schema or "public"
        tables = conn.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = %s",
                              (schema,)).fetchone()[0]
        return f"connected as {s.pg_user}; may create a schema: {can_create}; schema {schema} has {tables} tables"


def raw_container(s: Settings) -> str:
    store = build_output_store(s.blob_raw_url)
    store.put(PATH, BODY)
    return "wrote and read back" if store.get(PATH) == BODY else "wrote, but the read-back differs"


def output_container(url: str) -> str:
    build_output_store(url).put(PATH, BODY)
    return "wrote"


def backend(s: Settings) -> str:
    WrapperBackend(s.backend, s.backend_connect_timeout_s, s.backend_read_timeout_s)._valid_token()
    return "logged in (token received)"


def main() -> int:
    try:
        s = load_settings()
    except SettingsError as error:
        print(f"FAIL  settings: {error}")
        return 1
    print(f"PASS  settings: schema={s.pg_schema or 'public'}, host={s.pg_host}")
    results = [check("postgres", lambda: postgres(s))]
    if s.blob_raw_url:
        results.append(check("blob raw (p360-raw)", lambda: raw_container(s)))
    else:
        print("SKIP  blob raw: BLOB_OUTPUT_URL_RAW is not set")
    results += [check(f"blob output {source}", lambda url=url: output_container(url))
                for source, url in s.output_container_urls]
    if s.backend:
        results.append(check("backend login", lambda: backend(s)))
    else:
        print("SKIP  backend login: the BACKEND_* settings are not set")
    print("ALL PASS" if all(results) else "SOME FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
