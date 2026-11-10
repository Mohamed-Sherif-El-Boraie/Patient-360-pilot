"""Plays the wrapper: sends ONE symptom check to /api/v1/patient360/visits-symptoms and prints the answer.

    python tools/send_symptom.py --visit-id 2076334901 --patient-id 2142797381 --text "headache"
    python tools/send_symptom.py --visit-id 2076334901 --patient-id 2142797381 --dry-run

Inside the container the app listens on 127.0.0.1:8000 (the default of --url). Use the public address to test the
route the wrapper really takes.

This is a REAL symptom check: on a visit that is stored and still open it is saved and sent to AWS. The visit must
exist with the same patient id (else 404 VISIT_NOT_FOUND). A second call for the same visit is answered 200 and
nothing is saved again. Use --dry-run to see the body first.
"""
import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime

PATH = "/api/v1/patient360/visits-symptoms"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--visit-id", type=int, required=True)
    parser.add_argument("--patient-id", type=int, required=True)
    parser.add_argument("--text", default="test symptom check")
    parser.add_argument("--received-at", default=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    body = {"source": 3, "patient_id": args.patient_id, "visit_id": args.visit_id,
            "patient_symptoms": args.text, "symptom_received_at": args.received_at}
    target = args.url.rstrip("/") + PATH
    print(f"POST {target}\n{json.dumps(body, indent=2)}")
    if args.dry_run:
        return 0
    key = os.environ.get("INBOUND_API_KEY", "")        # the visit routes need none; sent only if set, never printed
    request = urllib.request.Request(
        target, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"} | ({"X-Api-Key": key} if key else {}))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print(f"-> {response.status} {response.read().decode()[:300]}")
    except urllib.error.HTTPError as error:
        print(f"-> {error.code} {error.read().decode()[:300]}")
    except urllib.error.URLError as error:
        print(f"-> no answer ({type(error.reason).__name__})")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
