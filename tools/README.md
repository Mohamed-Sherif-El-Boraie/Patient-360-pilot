# Local simulation

Runs the whole service on Docker Desktop with fake Backend, Azure Blob and AWS S3. Everything is a dev-only fake value.

```powershell
docker compose up --build          # first run builds the image; wait until the app logs it is running
uv run python tools/simulate.py    # in a second terminal: prints the steps and PASS or FAIL
docker compose down -v             # stop and delete all data (reset)
```

| Service | What it is | Host address |
|---|---|---|
| postgres | the database (user postgres, password test, db patient360) | 127.0.0.1:5434 |
| azurite | fake Azure Blob (Blob 1: the container `ai-input`, made by s3-setup and by the app) | 127.0.0.1:10000 |
| s3, s3-setup | fake AWS S3 (moto) and a one-shot that makes the four `p360-*` buckets and the `ai-input` container | 127.0.0.1:9000 |
| mock-wrapper | fake Backend wrapper (`tools/mock_wrapper.py`, runs inside the app image) | 127.0.0.1:8081 |
| app | the service; its entrypoint first runs `python -m patient360.migrate` and `python -m patient360.load_catalogue` (the catalogue loader moved from `tools/` into the package) | 127.0.0.1:8000 |

Mock wrapper extras: `GET /received` lists what it got; `POST /fail-next/3` makes the next 3 result posts answer 503
(to watch retries).

What `simulate.py` does: posts `urgent_mild.json`, waits for the input file in the fake S3, plays AWS and the Function
app (posts the answer file to `POST /api/v1/patient360/ai-results` with the headers `X-Source: urgent` and
`X-Blob-Path: <lane>/<yyyy>/<MM>/<dd>/<key>/05-output-merged.json`), then waits for the result at the mock wrapper.

`check_s3_layout.py` checks the S3 layout of all four buckets.

Not tested by the author (no Docker available): everything here, including the compose file, the Dockerfile and the
script. If the visit is accepted but never reaches the fake S3, check that the database catalogue knows the sample's tests.
