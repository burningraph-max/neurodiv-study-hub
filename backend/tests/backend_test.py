"""
Backend API tests for Coach Boost (FastAPI + MongoDB).
Covers: profile, tasks CRUD, flashcards SM-2, emotions, pomodoro, chat SSE streaming + history.
"""
import os
import time
import uuid
import json
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL")
if not BASE_URL:
    # Fall back to frontend .env if env var not set
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip()
                    break
    except Exception:
        pass
assert BASE_URL, "REACT_APP_BACKEND_URL is required"
BASE_URL = BASE_URL.rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def s():
    sess = requests.Session()
    sess.headers.update({"Content-Type": "application/json"})
    return sess


# ============== PROFILE ==============

class TestProfile:
    def test_get_profile_default(self, s):
        r = s.get(f"{API}/profile", timeout=20)
        assert r.status_code == 200, r.text
        data = r.json()
        for key in ["user_id", "name", "xp", "level", "streak", "badges"]:
            assert key in data
        assert data["level"] >= 1

    def test_patch_profile_xp_levels_badges(self, s):
        before = s.get(f"{API}/profile").json()
        xp_before = before["xp"]
        r = s.patch(f"{API}/profile", json={"xp_delta": 60})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["xp"] == xp_before + 60
        # Adding enough XP to trigger >= 50 badge
        assert "Première étape" in data["badges"]
        # Streak should be at least 1 after activity
        assert data["streak"] >= 1


# ============== TASKS ==============

class TestTasks:
    created_id = None

    def test_create_task(self, s):
        payload = {"title": "TEST_Math exo 3", "subject": "Maths", "duration_min": 20, "due_date": "2026-01-20"}
        r = s.post(f"{API}/tasks", json=payload)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["title"] == payload["title"]
        assert data["status"] == "todo"
        assert "id" in data
        TestTasks.created_id = data["id"]

    def test_list_tasks_contains(self, s):
        r = s.get(f"{API}/tasks")
        assert r.status_code == 200
        ids = [t["id"] for t in r.json()]
        assert TestTasks.created_id in ids

    def test_update_task_status_done(self, s):
        r = s.patch(f"{API}/tasks/{TestTasks.created_id}", json={"status": "done"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "done"
        # verify persistence
        r2 = s.get(f"{API}/tasks")
        match = [t for t in r2.json() if t["id"] == TestTasks.created_id]
        assert match and match[0]["status"] == "done"

    def test_delete_task(self, s):
        r = s.delete(f"{API}/tasks/{TestTasks.created_id}")
        assert r.status_code == 200
        assert r.json()["deleted"] == 1
        r2 = s.get(f"{API}/tasks")
        assert TestTasks.created_id not in [t["id"] for t in r2.json()]


# ============== FLASHCARDS / SM-2 ==============

class TestFlashcards:
    card_id = None

    def test_create_card(self, s):
        r = s.post(f"{API}/flashcards", json={"deck": "TEST_deck", "front": "2+2", "back": "4"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["front"] == "2+2"
        assert data["interval_days"] == 1
        assert data["ease"] == 2.5
        TestFlashcards.card_id = data["id"]

    def test_list_includes(self, s):
        r = s.get(f"{API}/flashcards")
        assert r.status_code == 200
        assert TestFlashcards.card_id in [c["id"] for c in r.json()]

    def test_review_quality_5_increases_interval(self, s):
        r = s.post(f"{API}/flashcards/{TestFlashcards.card_id}/review", json={"quality": 5})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["interval_days"] == 3  # initial 1 -> 3
        # ease should be >= 2.5 (no penalty for q=5)
        assert data["ease"] >= 2.5

    def test_review_quality_0_resets(self, s):
        # Push interval higher first
        s.post(f"{API}/flashcards/{TestFlashcards.card_id}/review", json={"quality": 5})
        before = s.get(f"{API}/flashcards").json()
        before_ease = [c for c in before if c["id"] == TestFlashcards.card_id][0]["ease"]
        r = s.post(f"{API}/flashcards/{TestFlashcards.card_id}/review", json={"quality": 0})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["interval_days"] == 1
        # ease should be lowered by 0.2 vs. its value before this review
        assert data["ease"] < before_ease, f"expected ease < {before_ease}, got {data['ease']}"
        assert data["ease"] >= 1.3

    def test_cleanup(self, s):
        if TestFlashcards.card_id:
            s.delete(f"{API}/flashcards/{TestFlashcards.card_id}")


# ============== EMOTIONS ==============

class TestEmotions:
    def test_create_and_list(self, s):
        marker = f"TEST_{uuid.uuid4().hex[:6]}"
        r = s.post(f"{API}/emotions", json={"moment": "before", "mood": 4, "energy": 3, "note": marker})
        assert r.status_code == 200, r.text
        created_id = r.json()["id"]
        r2 = s.get(f"{API}/emotions")
        assert r2.status_code == 200
        items = r2.json()
        # newest-first ordering: our just-created one should be in the list
        assert created_id in [e["id"] for e in items]
        # check sort order (created_at descending)
        if len(items) >= 2:
            assert items[0]["created_at"] >= items[1]["created_at"]


# ============== POMODORO ==============

class TestPomodoro:
    def test_log_and_list(self, s):
        r = s.post(f"{API}/pomodoro", json={"focus_min": 15, "break_min": 5, "completed": True})
        assert r.status_code == 200, r.text
        new_id = r.json()["id"]
        r2 = s.get(f"{API}/pomodoro")
        assert r2.status_code == 200
        assert new_id in [p["id"] for p in r2.json()]


# ============== CHAT (SSE streaming) ==============

class TestChat:
    session_id = f"TEST_sess_{uuid.uuid4().hex[:8]}"

    def test_stream_returns_sse_and_persists(self, s):
        url = f"{API}/chat/stream"
        payload = {"session_id": TestChat.session_id, "message": "Salut! Réponds juste 'OK' s'il te plaît."}
        with requests.post(url, json=payload, stream=True, timeout=60) as r:
            assert r.status_code == 200, r.text
            ct = r.headers.get("content-type", "")
            assert "text/event-stream" in ct, ct
            got_data = False
            got_done = False
            chunks = []
            for raw in r.iter_lines(decode_unicode=True):
                if raw is None:
                    continue
                if raw.startswith("data: "):
                    payload_line = raw[6:]
                    if payload_line == "[DONE]":
                        got_done = True
                        break
                    # New format: each delta is JSON-encoded
                    try:
                        decoded = json.loads(payload_line)
                    except Exception:
                        decoded = payload_line
                    if isinstance(decoded, str) and decoded:
                        chunks.append(decoded)
                        got_data = True
                elif raw.startswith("event: done"):
                    got_done = True
            assert got_data, "No data chunks received"
            assert got_done, "Stream did not terminate with done event"
        # Wait briefly for async persistence
        time.sleep(1.0)
        h = s.get(f"{API}/chat/history/{TestChat.session_id}")
        assert h.status_code == 200
        msgs = h.json()
        roles = [m["role"] for m in msgs]
        assert "user" in roles and "assistant" in roles, f"Roles seen: {roles}"

    def test_multiturn_context(self, s):
        url = f"{API}/chat/stream"
        payload = {"session_id": TestChat.session_id, "message": "Quelle était ma question précédente en un mot?"}
        with requests.post(url, json=payload, stream=True, timeout=90) as r:
            assert r.status_code == 200
            text_parts = []
            for raw in r.iter_lines(decode_unicode=True):
                if raw and raw.startswith("data: "):
                    p = raw[6:]
                    if p == "[DONE]":
                        break
                    try:
                        decoded = json.loads(p)
                    except Exception:
                        decoded = p
                    if isinstance(decoded, str):
                        text_parts.append(decoded)
        full = "".join(text_parts)
        assert len(full) > 0
        # History now should have >=4 messages
        h = s.get(f"{API}/chat/history/{TestChat.session_id}").json()
        assert len(h) >= 4

    def test_clear_history(self, s):
        r = s.delete(f"{API}/chat/history/{TestChat.session_id}")
        assert r.status_code == 200
        h = s.get(f"{API}/chat/history/{TestChat.session_id}").json()
        assert h == []
