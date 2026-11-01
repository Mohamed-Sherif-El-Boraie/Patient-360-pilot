"""Plays the Backend: sends the four sample sources to a running app, to test every flow end to end.

    python tools/send_samples.py --url http://127.0.0.1:8000 --fresh

The visit routes need no API key (sent only if INBOUND_API_KEY is set, never printed). The samples are in tools/samples:
urgent_source.json (source 1), on_demand_source.json (4), batch_source.json (2) and symptom_checker_source.json (3).

  --only urgent,on-demand,batch,symptom   send only these (default: all, in this order)
  --fresh        new visit and patient ids on every run (a second run with the same ids is a duplicate, answered 200)
  --batch-count  how many copies of the batch sample to send (default 120, each a different visit). The app makes
                 one JSONL of BATCH_SIZE visits (default 120) per lane, so 120 copies make exactly one batch
  --dry-run      print what would be sent, send nothing

This starts the real automation: the visits go to S3 and AWS answers them. Use it only when AWS agreed to the test.
The symptom check needs the answer of the on-demand visit first; the app keeps it waiting until that answer arrives.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SAMPLES = Path(__file__).resolve().parent / "samples"
FILES = {"urgent": "urgent_source.json", "on-demand": "on_demand_source.json", "batch": "batch_source.json",
         "symptom": "symptom_checker_source.json"}
PATHS = {"symptom": "/api/v1/patient360/visits-symptoms"}          # the others go to /visits
ORDER = ("urgent", "on-demand", "batch", "symptom")


def load(name: str, offset: int, step: int = 0) -> dict:
    """A sample with its ids moved: ``offset`` is the same for the whole run (so the symptom check finds the on-demand
    visit), ``step`` is the copy number of a batch visit."""
    body = json.loads((SAMPLES / FILES[name]).read_text(encoding="utf-8"))
    body["visit_id"] += offset + step
    body["patient_id"] += offset + step
    return body


def post(base: str, name: str, body: dict, key: str) -> str:
    request = urllib.request.Request(
        base.rstrip("/") + PATHS.get(name, "/api/v1/patient360/visits"), data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"} | ({"X-Api-Key": key} if key else {}), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return f"{response.status} {response.read().decode()[:120]}"
    except urllib.error.HTTPError as error:
        return f"{error.code} {error.read().decode()[:200]}"
    except urllib.error.URLError as error:
        return f"no answer ({type(error.reason).__name__})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True)
    parser.add_argument("--only", default=",".join(ORDER))
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--batch-count", type=int, default=120)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    chosen = [name for name in ORDER if name in args.only.split(",")]
    key = os.environ.get("INBOUND_API_KEY", "")      # the visit routes ignore it; harmless to send
    offset = int(time.time()) * 1000 if args.fresh else 0
    failed = 0
    for name in chosen:
        copies = args.batch_count if name == "batch" else 1
        outcomes: dict[str, int] = {}
        for step in range(copies):
            body = load(name, offset, step)
            answer = "dry run" if args.dry_run else post(args.url, name, body, key)
            status = answer.split(" ")[0]
            outcomes[status] = outcomes.get(status, 0) + 1
            if copies == 1:
                print(f"{name:10} source={body['source']} visit={body['visit_id']}  ->  {answer}")
        if copies > 1:
            first, last = load(name, offset)["visit_id"], load(name, offset, copies - 1)["visit_id"]
            print(f"{name:10} {copies} visits {first}..{last}  ->  {outcomes}")
        failed += sum(count for status, count in outcomes.items() if status[:1] != "2" and status != "dry")
    print("done" if not failed else f"{failed} request(s) were not accepted (2xx)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
