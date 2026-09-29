"""联合消纳计划 HTTP JSON 接口测试。"""

from __future__ import annotations

import json
import sqlite3
import unittest

from renewable_dispatch.api import JsonApplication
from renewable_dispatch.service import DispatchService


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(DispatchService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_json_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_actor_required(self) -> None:
        payload = json.dumps({
            "horizon_id": "h1", "starts_at": "2026-09-29T00:00:00Z",
            "interval_minutes": 60, "slot_count": 2,
        }).encode()
        response = self.app.handle("POST", "/horizons", body=payload)
        self.assertEqual(response.status, 422)
        self.assertIn("X-Actor-Id", response.body["error"]["message"])

    def test_full_http_flow(self) -> None:
        def post(path: str, payload: dict, actor: str = "plan"):
            return self.app.handle(
                "POST", path, {"x-actor-id": actor},
                json.dumps(payload).encode("utf-8"),
            )

        def get(path: str, actor: str = "dispatch"):
            return self.app.handle("GET", path, {"x-actor-id": actor})

        self.assertEqual(post("/users", {
            "user_id": "plan", "display_name": "规划", "role": "planner"}).status, 201)
        self.assertEqual(post("/users", {
            "user_id": "dispatch", "display_name": "调度", "role": "dispatcher"}).status, 201)
        self.assertEqual(post("/users", {
            "user_id": "operator", "display_name": "现场", "role": "operator"}).status, 201)
        self.assertEqual(post("/users", {
            "user_id": "audit", "display_name": "审计", "role": "auditor"}).status, 201)

        self.assertEqual(post("/horizons", {
            "horizon_id": "h1", "starts_at": "2026-09-29T00:00:00Z",
            "interval_minutes": 60, "slot_count": 2}).status, 201)
        self.assertEqual(post("/forecasts", {
            "forecast_id": "f1", "resource_id": "w1", "resource_kind": "wind",
            "version": 1, "interval_minutes": 60, "power_mw": ["0", "40"],
            "observed_at": "2026-09-28T20:00:00Z"}).status, 201)
        self.assertEqual(post("/storage", {
            "storage_id": "b1", "storage_kind": "battery", "interval_minutes": 60,
            "capacity_mwh": "10", "max_charge_mw": "10", "max_discharge_mw": "10"}).status, 201)
        self.assertEqual(post("/storage/windows", {
            "storage_id": "b1", "version": 1, "interval_minutes": 60,
            "initial_soc_mwh": "10", "available": [True, True], "reason": "正常"}).status, 201)
        self.assertEqual(post("/lines", {
            "line_id": "l1", "interval_minutes": 60, "rated_mw": "50"}).status, 201)
        self.assertEqual(post("/lines/capabilities", {
            "line_id": "l1", "version": 1, "interval_minutes": 60, "rated_mw": "50",
            "limits_mw": [None, None], "reason": "正常"}).status, 201)
        self.assertEqual(post("/loads", {
            "load_id": "p1", "load_kind": "industrial", "interval_minutes": 60}).status, 201)
        self.assertEqual(post("/loads/commitments", {
            "load_id": "p1", "version": 1, "interval_minutes": 60,
            "firm_mw": ["20", "20"], "curtailable_mw": ["0", "0"],
            "reductions": [None, None], "reason": "基线"}).status, 201)

        generated = post("/plans/generate", {"plan_id": "plan-1", "horizon_id": "h1"})
        self.assertEqual(generated.status, 201)
        self.assertEqual(generated.body["revision"], 1)

        confirmed = post("/plans/plan-1/confirm", {"expected_revision": 1}, actor="dispatch")
        self.assertEqual(confirmed.status, 200)
        self.assertEqual(len(confirmed.body["locked_resources"]), 3)

        plan = get("/plans/plan-1", actor="audit")
        self.assertEqual(plan.status, 200)
        self.assertEqual(plan.body["state"], "confirmed")
        self.assertEqual(len(plan.body["periods"]), 2)

        operations = get("/operations/h1")
        self.assertEqual(operations.status, 200)
        self.assertEqual(operations.body["plan_id"], "plan-1")
        self.assertIn("line_margins", operations.body["periods"][0])

        recovered = get("/plans/recover")
        self.assertEqual(recovered.status, 200)
        self.assertTrue(recovered.body["plans"])

        verify = get("/plans/plan-1/verify?revision=1", actor="audit")
        self.assertEqual(verify.status, 200)
        self.assertTrue(verify.body["replay_matches"])

        chain = get("/audit/chain", actor="audit")
        self.assertEqual(chain.status, 200)
        self.assertTrue(chain.body["valid"])

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope", {"x-actor-id": "audit"})
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
