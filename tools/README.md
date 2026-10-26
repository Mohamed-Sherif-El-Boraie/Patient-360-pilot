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
| azurite | fake Azure Blob and Queue (the two containers are made by simulate.py, the event queues by s3-setup) | 127.0.0.1:10000 (blob), 10001 (queue) |
| s3, s3-setup | fake AWS S3 (moto) and a one-shot that makes the four `p360-*` buckets and the event queues | 127.0.0.1:9000 |
| mock-wrapper | fake Backend wrapper (`tools/mock_wrapper.py`, runs inside the app image) | 127.0.0.1:8081 |
| migrate | one-shot: `python -m patient360.migrate` | none |
| app | the service | 127.0.0.1:8000 |

Mock wrapper extras: `GET /received` lists what it got; `POST /fail-next/3` makes the next 3 result posts answer 503
(to watch retries).

What `simulate.py` does: posts `urgent_mild.json`, waits for the file in the fake S3, plays AWS (writes the answer to
Azurite), plays Event Grid (writes the event into the `blob-events` queue, which the app reads), then waits for the result at
the mock wrapper.

Not tested by the author (no Docker available): everything here, including the compose file, the Dockerfile and the
script. If the visit is accepted but never reaches the fake S3, check that the database catalogue knows the sample's tests.
