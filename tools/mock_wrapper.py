"""A fake Backend wrapper for the local simulation. Keeps what it receives in memory. Never logs a body."""
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI()
received: list[dict] = []
state = {"fail": 0}
CALLER_HEADERS = ("CallerSource", "CallerUserName", "CallerSourceKey")
OK = {"code": -1, "message": "Success"}


@app.post("/API/v2/Auth/Callerlogin")
async def login(request: Request):
    missing = [h for h in CALLER_HEADERS if h not in request.headers]
    body = await request.json()
    if missing or not all(body.get(k) for k in ("username", "password")):
        raise HTTPException(400, "missing caller headers or login fields")
    return {"response": OK, "data": {"CallerCredential": {"Caller": {"TokenExpiredMin": 480}, "token": "mock-token"}}}


@app.post("/API/v2/Patient360/Patient360011")
async def post_result(request: Request, authorization: str | None = Header(None)):
    if authorization != "Bearer mock-token":
        raise HTTPException(401, "bad token")
    if state["fail"] > 0:
        state["fail"] -= 1
        return JSONResponse({"response": {"code": 0, "message": "down"}}, status_code=503)
    received.append(await request.json())
    return {"response": OK, "data": {"Patient360Loader": {"ID": len(received), "Action": "Insert"}}}


@app.get("/received")
def get_received():
    return received


@app.post("/fail-next/{n}")
def fail_next(n: int):
    state["fail"] = n
    return {"failing_next": n}
