"""
Backend tests for photo upload + Socratic Claude vision analysis on tasks.
Covers: image upload (POST/PATCH/clear), list strips image, image GET, MIME rejection,
        SSE /analyze with structure + no-direct-answer + persistence.
"""
import os
import io
import re
import time
import json
import base64
import uuid

import pytest
import requests
from PIL import Image, ImageDraw, ImageFont

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip()
                break
assert BASE_URL, "REACT_APP_BACKEND_URL is required"
API = BASE_URL.rstrip("/") + "/api"


def _make_image_data_url(text="3x + 7 = 22", fmt="JPEG"):
    """Generate a JPEG/PNG/WEBP image containing real visual features (text)."""
    img = Image.new("RGB", (640, 360), color=(245, 245, 245))
    draw = ImageDraw.Draw(img)
    # Some shapes for non-uniform variance
    draw.rectangle([20, 20, 620, 340], outline=(20, 20, 20), width=4)
    draw.line([(20, 180), (620, 180)], fill=(180, 30, 30), width=3)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 64)
    except Exception:
        font = ImageFont.load_default()
    draw.text((80, 130), text, fill=(10, 10, 10), font=font)
    draw.text((80, 230), "Exercice 5", fill=(60, 60, 60), font=font)
    buf = io.BytesIO()
    img.save(buf, format=fmt, quality=85)
    raw_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    mime = "image/jpeg" if fmt == "JPEG" else f"image/{fmt.lower()}"
    return f"data:{mime};base64,{raw_b64}", raw_b64, mime


@pytest.fixture(scope="module")
def s():
    sess = requests.Session()
    sess.headers.update({"Content-Type": "application/json"})
    return sess


@pytest.fixture(scope="module")
def jpeg_image():
    return _make_image_data_url("3x + 7 = 22", "JPEG")


# -------- Upload + list strip --------
class TestTaskImageUpload:
    created_id = None

    def test_create_task_with_image(self, s, jpeg_image):
        data_url, raw_b64, _mime = jpeg_image
        r = s.post(f"{API}/tasks", json={
            "title": "TEST_IMG equation", "subject": "Maths", "duration_min": 10,
            "image_base64": data_url,
        })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["has_image"] is True
        # Response model shouldn't include image_base64
        assert "image_base64" not in data
        TestTaskImageUpload.created_id = data["id"]

    def test_list_excludes_image_base64(self, s):
        r = s.get(f"{API}/tasks")
        assert r.status_code == 200
        items = r.json()
        match = [t for t in items if t["id"] == TestTaskImageUpload.created_id]
        assert match, "task not in list"
        t = match[0]
        assert t["has_image"] is True
        # The list endpoint strips image_base64
        assert "image_base64" not in t or not t.get("image_base64")

    def test_get_task_image(self, s, jpeg_image):
        _data_url, raw_b64, mime = jpeg_image
        r = s.get(f"{API}/tasks/{TestTaskImageUpload.created_id}/image")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mime"] == mime
        assert body["image_base64"] == raw_b64

    def test_get_task_image_404_when_no_image(self, s):
        # Create a no-image task and try to fetch its image
        r = s.post(f"{API}/tasks", json={"title": "TEST_no_image"})
        tid = r.json()["id"]
        r2 = s.get(f"{API}/tasks/{tid}/image")
        assert r2.status_code == 404
        s.delete(f"{API}/tasks/{tid}")


# -------- PATCH attach / clear --------
class TestTaskImagePatch:
    def test_patch_attach_then_clear(self, s, jpeg_image):
        data_url, _raw_b64, _mime = jpeg_image
        # Create empty task
        r = s.post(f"{API}/tasks", json={"title": "TEST_patch_img"})
        tid = r.json()["id"]
        try:
            # Pretend it already has stale analysis
            s.patch(f"{API}/tasks/{tid}", json={"notes": "n"})  # benign
            # Attach
            r2 = s.patch(f"{API}/tasks/{tid}", json={"image_base64": data_url})
            assert r2.status_code == 200, r2.text
            assert r2.json()["has_image"] is True
            assert r2.json().get("analysis") is None  # reset
            # Verify image fetchable
            ri = s.get(f"{API}/tasks/{tid}/image")
            assert ri.status_code == 200
            # Clear
            r3 = s.patch(f"{API}/tasks/{tid}", json={"clear_image": True})
            assert r3.status_code == 200
            assert r3.json()["has_image"] is False
            assert r3.json().get("analysis") is None
            r4 = s.get(f"{API}/tasks/{tid}/image")
            assert r4.status_code == 404
        finally:
            s.delete(f"{API}/tasks/{tid}")


# -------- MIME rejection --------
class TestImageMimeRejection:
    def test_reject_gif(self, s):
        # Tiny valid-ish GIF base64 (header only, doesn't matter — server rejects on mime prefix)
        bogus = "data:image/gif;base64,R0lGODlhAQABAAAAACw="
        r = s.post(f"{API}/tasks", json={"title": "TEST_gif_reject", "image_base64": bogus})
        assert r.status_code == 400, r.text

    def test_reject_invalid_data_url(self, s):
        r = s.post(f"{API}/tasks", json={"title": "TEST_bad", "image_base64": "data:notvalid"})
        assert r.status_code == 400

    def test_accept_png(self, s):
        data_url, _b64, _m = _make_image_data_url("Hello", "PNG")
        r = s.post(f"{API}/tasks", json={"title": "TEST_png_ok", "image_base64": data_url})
        assert r.status_code == 200, r.text
        tid = r.json()["id"]
        s.delete(f"{API}/tasks/{tid}")

    def test_accept_webp(self, s):
        data_url, _b64, _m = _make_image_data_url("Hi", "WEBP")
        r = s.post(f"{API}/tasks", json={"title": "TEST_webp_ok", "image_base64": data_url})
        assert r.status_code == 200, r.text
        tid = r.json()["id"]
        s.delete(f"{API}/tasks/{tid}")


# -------- SSE /analyze --------
class TestAnalyzeSSE:
    def test_analyze_streams_socratic_structure(self, s, jpeg_image):
        data_url, _raw_b64, _mime = jpeg_image
        # Fresh task with the equation
        r = s.post(f"{API}/tasks", json={
            "title": "TEST_ANALYZE equation 3x+7=22",
            "subject": "Maths",
            "image_base64": data_url,
        })
        assert r.status_code == 200, r.text
        tid = r.json()["id"]
        try:
            url = f"{API}/tasks/{tid}/analyze"
            with requests.post(url, stream=True, timeout=120) as resp:
                assert resp.status_code == 200, resp.text
                ct = resp.headers.get("content-type", "")
                assert "text/event-stream" in ct
                got_done = False
                chunks = []
                for raw in resp.iter_lines(decode_unicode=True):
                    if raw is None:
                        continue
                    if raw.startswith("data: "):
                        payload = raw[6:]
                        if payload == "[DONE]":
                            got_done = True
                            break
                        # Each delta is JSON-encoded; decode to preserve newlines
                        try:
                            decoded = json.loads(payload)
                        except Exception:
                            decoded = payload
                        if isinstance(decoded, str):
                            chunks.append(decoded)
                    elif raw.startswith("event: done"):
                        got_done = True
                assert got_done, "no done event"
            streamed = "".join(chunks)
            assert len(streamed) > 40, f"stream too short: {streamed!r}"
            # NEW: streamed text (chunks reassembled) must contain all 5 markers
            for marker in ["📖", "🧩", "🔑", "💡", "🤔"]:
                assert marker in streamed, (
                    f"missing structure marker {marker} in LIVE stream:\n{streamed[:500]}"
                )

            # Persistence: wait briefly and re-fetch task. Persisted analysis is
            # the canonical full text (server accumulates raw deltas).
            time.sleep(2.0)
            r2 = s.get(f"{API}/tasks")
            match = [t for t in r2.json() if t["id"] == tid]
            assert match
            saved = match[0].get("analysis") or ""
            assert len(saved) > 80, f"analysis not persisted: {saved!r}"
            # Required Socratic structure markers in the persisted analysis
            for marker in ["📖", "🧩", "🔑", "💡", "🤔"]:
                assert marker in saved, f"missing structure marker {marker} in persisted analysis:\n{saved}"
            # Stream and persisted analysis should be byte-equivalent (ignore whitespace trim)
            assert streamed.strip() == saved.strip(), (
                f"streamed vs persisted mismatch:\nstreamed: {streamed!r}\nsaved: {saved!r}"
            )
            # Must NOT contain the literal numeric solution x = 5
            for pat in [r"x\s*=\s*5\b"]:
                assert not re.search(pat, saved), (
                    f"Direct answer leaked (pattern={pat}) in persisted analysis:\n{saved}"
                )
            # Also assert no standalone "= 5" near end (defensive)
            assert "= 5" not in saved.split("🤔")[0], (
                f"Direct answer '= 5' leaked before closing section:\n{saved}"
            )
        finally:
            s.delete(f"{API}/tasks/{tid}")

    def test_analyze_404_without_image(self, s):
        r = s.post(f"{API}/tasks", json={"title": "TEST_no_img_analyze"})
        tid = r.json()["id"]
        try:
            rr = requests.post(f"{API}/tasks/{tid}/analyze", timeout=30)
            assert rr.status_code == 400
        finally:
            s.delete(f"{API}/tasks/{tid}")


# -------- Cleanup --------
def teardown_module(_module):
    try:
        sess = requests.Session()
        r = sess.get(f"{API}/tasks")
        for t in r.json():
            if t["title"].startswith("TEST_"):
                sess.delete(f"{API}/tasks/{t['id']}")
    except Exception:
        pass
