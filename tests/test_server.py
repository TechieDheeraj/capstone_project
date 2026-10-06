import time

from fastapi.testclient import TestClient

from refund_desk.server import create_app


def wait_for(client, run_id, statuses, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/api/runs/{run_id}")
        if r.status_code == 200 and r.json()["run"]["status"] in statuses:
            return r.json()
        time.sleep(0.1)
    raise AssertionError(f"run did not reach {statuses}")


def test_web_api_runs_a_ticket_with_approvals(app):
    client = TestClient(create_app(app))
    assert client.get("/api/health").json()["mode"] == "offline"
    run_id = client.post("/api/runs", json={"ticket_id": "T-102"}).json()["run_id"]
    d = wait_for(client, run_id, {"waiting_approval"})
    email = [a for a in d["approvals"] if a["status"] == "pending"][0]
    assert client.post(f"/api/approvals/{email['id']}", json={"approved": True}).status_code == 200
    wait_for(client, run_id, {"resolved"})
    events = client.get(f"/api/runs/{run_id}/events").text
    assert "run_finished" in events and "tool_start" in events
    assert client.post(f"/api/runs/{run_id}/feedback", json={"rating": 1}).json()["ok"]
    assert client.get("/").status_code == 200
