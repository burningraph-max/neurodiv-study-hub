from fastapi import FastAPI, APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import json
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional
import uuid
from datetime import datetime, timezone, timedelta

from emergentintegrations.llm.chat import LlmChat, UserMessage, TextDelta, StreamDone, ImageContent

from coach_prompt import SYSTEM_PROMPT, ANALYSIS_PROMPT
from boosts import BOOSTS
import random

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

EMERGENT_LLM_KEY = os.environ.get('EMERGENT_LLM_KEY')

# Themes (4 free + 4 unlockable by level)
THEMES_META = {
    "orange":  {"name": "Flame",   "min_level": 1, "hex": "#f97316"},
    "blue":    {"name": "Boost",   "min_level": 1, "hex": "#3b82f6"},
    "purple":  {"name": "Nova",    "min_level": 1, "hex": "#a855f7"},
    "emerald": {"name": "Turbo",   "min_level": 1, "hex": "#10b981"},
    "neon":    {"name": "Neon",    "min_level": 3, "hex": "#ec4899"},
    "stadium": {"name": "Stadium", "min_level": 5, "hex": "#eab308"},
    "retro":   {"name": "Retro",   "min_level": 7, "hex": "#ef4444"},
    "galaxy":  {"name": "Galaxy",  "min_level": 10, "hex": "#8b5cf6"},
}


app = FastAPI()
api_router = APIRouter(prefix="/api")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


# ============== MODELS ==============

class Profile(BaseModel):
    user_id: str = "default"
    name: str = "Champion"
    avatar_color: str = "orange"
    onboarded: bool = False
    xp: int = 0
    level: int = 1
    streak: int = 0
    last_active_date: Optional[str] = None
    badges: List[str] = []
    unlocked_themes: List[str] = ["orange", "blue", "purple", "emerald"]
    share_token: Optional[str] = None
    reminder_hour: Optional[int] = None  # 0-23 or None to disable


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    avatar_color: Optional[str] = None
    onboarded: Optional[bool] = None
    xp_delta: Optional[int] = None
    reminder_hour: Optional[int] = None


class Task(BaseModel):
    id: str = Field(default_factory=new_id)
    title: str
    subject: Optional[str] = None
    duration_min: int = 15
    due_date: Optional[str] = None  # YYYY-MM-DD
    status: str = "todo"  # todo | doing | done
    notes: Optional[str] = None
    has_image: bool = False
    analysis: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)


class TaskCreate(BaseModel):
    title: str
    subject: Optional[str] = None
    duration_min: int = 15
    due_date: Optional[str] = None
    notes: Optional[str] = None
    image_base64: Optional[str] = None  # data URL or raw base64


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    subject: Optional[str] = None
    duration_min: Optional[int] = None
    due_date: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    image_base64: Optional[str] = None
    clear_image: Optional[bool] = None


class Flashcard(BaseModel):
    id: str = Field(default_factory=new_id)
    deck: str = "Général"
    front: str
    back: str
    interval_days: int = 1
    ease: float = 2.5
    next_review: str = Field(default_factory=now_iso)
    created_at: str = Field(default_factory=now_iso)


class FlashcardCreate(BaseModel):
    deck: str = "Général"
    front: str
    back: str


class FlashcardReview(BaseModel):
    quality: int  # 0 (oublié) | 3 (dur) | 4 (ok) | 5 (facile)


class GeneratedCard(BaseModel):
    front: str
    back: str


class GenerateCardsRequest(BaseModel):
    image_base64: str
    deck: str = "Auto"
    subject: Optional[str] = None
    count_hint: int = 8


class BatchCardsRequest(BaseModel):
    deck: str = "Général"
    cards: List[GeneratedCard]


class EmotionCheckin(BaseModel):
    id: str = Field(default_factory=new_id)
    moment: str  # "before" | "after"
    mood: int  # 1-5
    energy: int  # 1-5
    note: Optional[str] = None
    task_id: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)


class EmotionCreate(BaseModel):
    moment: str
    mood: int
    energy: int
    note: Optional[str] = None
    task_id: Optional[str] = None


class PomodoroSession(BaseModel):
    id: str = Field(default_factory=new_id)
    focus_min: int = 15
    break_min: int = 5
    task_id: Optional[str] = None
    completed: bool = True
    created_at: str = Field(default_factory=now_iso)


class PomodoroCreate(BaseModel):
    focus_min: int = 15
    break_min: int = 5
    task_id: Optional[str] = None
    completed: bool = True


class ChatMessage(BaseModel):
    id: str = Field(default_factory=new_id)
    session_id: str
    role: str  # user | assistant
    content: str
    created_at: str = Field(default_factory=now_iso)


class ChatRequest(BaseModel):
    session_id: str
    message: str
    image_base64: Optional[str] = None
    task_id: Optional[str] = None  # If set, attach the task's image automatically


# ============== PROFILE ==============

async def _get_or_create_profile() -> dict:
    p = await db.profiles.find_one({"user_id": "default"}, {"_id": 0})
    if not p:
        p = Profile().model_dump()
        await db.profiles.insert_one(p)
    if not p.get("share_token"):
        p["share_token"] = new_id()
        await db.profiles.update_one({"user_id": "default"}, {"$set": {"share_token": p["share_token"]}})
    return p


def _xp_to_level(xp: int) -> int:
    # niveau = 1 + sqrt(xp / 50), cap par marche douce
    lvl = 1
    needed = 100
    remaining = xp
    while remaining >= needed:
        remaining -= needed
        lvl += 1
        needed = int(needed * 1.3)
    return lvl


@api_router.get("/profile", response_model=Profile)
async def get_profile():
    p = await _get_or_create_profile()
    return Profile(**p)


@api_router.patch("/profile", response_model=Profile)
async def update_profile(update: ProfileUpdate):
    p = await _get_or_create_profile()
    if update.name is not None:
        p["name"] = update.name
    if update.avatar_color is not None:
        p["avatar_color"] = update.avatar_color
    if update.onboarded is not None:
        p["onboarded"] = update.onboarded
    if update.reminder_hour is not None:
        p["reminder_hour"] = max(0, min(23, update.reminder_hour))
    if update.xp_delta:
        p["xp"] = max(0, p.get("xp", 0) + update.xp_delta)
        p["level"] = _xp_to_level(p["xp"])

    today = datetime.now(timezone.utc).date().isoformat()
    last = p.get("last_active_date")
    if last != today:
        if last:
            yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
            p["streak"] = p.get("streak", 0) + 1 if last == yesterday else 1
        else:
            p["streak"] = 1
        p["last_active_date"] = today

    # badges
    badges = set(p.get("badges", []))
    if p["xp"] >= 50:
        badges.add("Première étape")
    if p["streak"] >= 3:
        badges.add("Régulier")
    if p["streak"] >= 7:
        badges.add("Une semaine")
    if p["streak"] >= 14:
        badges.add("Marathon")
    if p["level"] >= 5:
        badges.add("Niveau 5")
    if p["level"] >= 10:
        badges.add("Niveau 10")
    p["badges"] = sorted(badges)

    # Auto-unlock themes based on level
    unlocked = set(p.get("unlocked_themes", []) or ["orange", "blue", "purple", "emerald"])
    level = p.get("level", 1)
    for key, meta in THEMES_META.items():
        if level >= meta["min_level"]:
            unlocked.add(key)
    p["unlocked_themes"] = sorted(unlocked)

    await db.profiles.update_one({"user_id": "default"}, {"$set": p}, upsert=True)
    return Profile(**p)


# ============== TASKS ==============

ALLOWED_IMG_PREFIX = ("data:image/jpeg", "data:image/png", "data:image/webp")


def _normalize_image(b64: str) -> tuple[str, str]:
    """Returns (raw_base64, mime). Strips data URL prefix."""
    if not b64:
        return "", "image/jpeg"
    if b64.startswith("data:"):
        # data:image/jpeg;base64,xxxx
        try:
            header, payload = b64.split(",", 1)
            mime = header.split(";")[0].split(":")[1]
            if mime not in ("image/jpeg", "image/png", "image/webp"):
                raise HTTPException(400, "Image format must be JPEG, PNG or WEBP")
            return payload, mime
        except (ValueError, IndexError):
            raise HTTPException(400, "Invalid data URL")
    return b64, "image/jpeg"


@api_router.post("/tasks", response_model=Task)
async def create_task(data: TaskCreate):
    payload = data.model_dump()
    image_b64 = payload.pop("image_base64", None)
    task = Task(**payload)
    doc = task.model_dump()
    if image_b64:
        raw, mime = _normalize_image(image_b64)
        doc["image_base64"] = raw
        doc["image_mime"] = mime
        doc["has_image"] = True
        task.has_image = True
    await db.tasks.insert_one(doc)
    return task


@api_router.get("/tasks", response_model=List[Task])
async def list_tasks(status: Optional[str] = None, date: Optional[str] = None):
    q = {}
    if status:
        q["status"] = status
    if date:
        q["due_date"] = date
    # Exclude heavy image_base64 from list
    docs = await db.tasks.find(q, {"_id": 0, "image_base64": 0, "image_mime": 0}).sort("created_at", -1).to_list(500)
    return [Task(**d) for d in docs]


@api_router.get("/tasks/{task_id}/image")
async def get_task_image(task_id: str):
    doc = await db.tasks.find_one({"id": task_id}, {"_id": 0, "image_base64": 1, "image_mime": 1})
    if not doc or not doc.get("image_base64"):
        raise HTTPException(404, "No image")
    return {"image_base64": doc["image_base64"], "mime": doc.get("image_mime", "image/jpeg")}


@api_router.patch("/tasks/{task_id}", response_model=Task)
async def update_task(task_id: str, update: TaskUpdate):
    doc = await db.tasks.find_one({"id": task_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Task not found")
    patch = {k: v for k, v in update.model_dump().items() if v is not None and k not in ("image_base64", "clear_image")}
    if update.image_base64:
        raw, mime = _normalize_image(update.image_base64)
        patch["image_base64"] = raw
        patch["image_mime"] = mime
        patch["has_image"] = True
        patch["analysis"] = None  # reset analysis when image changes
    if update.clear_image:
        patch["image_base64"] = None
        patch["image_mime"] = None
        patch["has_image"] = False
        patch["analysis"] = None
    if patch:
        await db.tasks.update_one({"id": task_id}, {"$set": patch})
        doc.update(patch)
    # Strip image fields before returning
    doc.pop("image_base64", None)
    doc.pop("image_mime", None)
    return Task(**doc)


@api_router.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    r = await db.tasks.delete_one({"id": task_id})
    return {"deleted": r.deleted_count}


@api_router.post("/tasks/{task_id}/analyze")
async def analyze_task(task_id: str):
    if not EMERGENT_LLM_KEY:
        raise HTTPException(500, "EMERGENT_LLM_KEY missing")

    doc = await db.tasks.find_one({"id": task_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Task not found")
    image_b64 = doc.get("image_base64")
    if not image_b64:
        raise HTTPException(400, "No image attached")

    title = doc.get("title") or ""
    subject = doc.get("subject") or ""
    user_text = (
        f"Voici la photo d'un devoir.\n"
        f"Matière indiquée : {subject or 'non précisée'}\n"
        f"Titre : {title or 'non précisé'}\n\n"
        f"Analyse-le selon ta structure obligatoire. Aide-le à comprendre, sans donner la réponse."
    )

    chat = LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=f"analyze_{task_id}_{uuid.uuid4().hex[:8]}",
        system_message=ANALYSIS_PROMPT,
    ).with_model("anthropic", "claude-sonnet-4-6")

    image_content = ImageContent(image_base64=image_b64)

    async def event_generator():
        full = []
        try:
            async for event in chat.stream_message(
                UserMessage(text=user_text, file_contents=[image_content])
            ):
                if isinstance(event, TextDelta):
                    full.append(event.content)
                    yield f"data: {json.dumps(event.content)}\n\n"
                elif isinstance(event, StreamDone):
                    break
        except Exception as e:
            logging.exception("Analyze stream error")
            yield f"data: {json.dumps(f'[ERREUR] {str(e)}')}\n\n"
        finally:
            text = "".join(full).strip()
            if text:
                await db.tasks.update_one({"id": task_id}, {"$set": {"analysis": text}})
            yield "event: done\ndata: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============== FLASHCARDS ==============

@api_router.post("/flashcards", response_model=Flashcard)
async def create_flashcard(data: FlashcardCreate):
    card = Flashcard(**data.model_dump())
    await db.flashcards.insert_one(card.model_dump())
    return card


@api_router.post("/flashcards/generate", response_model=List[GeneratedCard])
async def generate_flashcards(req: GenerateCardsRequest):
    if not EMERGENT_LLM_KEY:
        raise HTTPException(500, "EMERGENT_LLM_KEY missing")
    raw, _mime = _normalize_image(req.image_base64)

    user_prompt = (
        f"Photo d'un cours/devoir d'un ado de 14 ans (TDAH, collège suisse, niveau 10H Harmos).\n"
        f"Matière : {req.subject or 'non précisée'}\n"
        f"Génère exactement {req.count_hint} flashcards Q/R pour l'aider à mémoriser les points clés du contenu visible sur la photo.\n\n"
        "RÈGLES :\n"
        "- Questions COURTES (max 12 mots), précises, à réponse fermée.\n"
        "- Réponses COURTES (max 15 mots), 1 phrase max.\n"
        "- Mélange : définitions, dates, vocabulaire, formules, exemples concrets.\n"
        "- Évite les questions oui/non et les questions trop génériques.\n"
        "- Si la photo contient peu d'infos, génère moins de cartes.\n"
        "- Si la photo est illisible ou pas un cours, renvoie un tableau vide [].\n\n"
        "FORMAT DE SORTIE (STRICT) : JSON UNIQUEMENT, un tableau d'objets, AUCUN texte autour, AUCUN markdown.\n"
        '[{"front": "Question ?", "back": "Réponse"}, ...]'
    )

    chat = LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=f"gen_{uuid.uuid4().hex[:10]}",
        system_message="Tu génères des flashcards éducatives. Réponds STRICTEMENT en JSON valide, sans texte autour, sans markdown.",
    ).with_model("anthropic", "claude-sonnet-4-6")

    full = []
    try:
        async for event in chat.stream_message(
            UserMessage(text=user_prompt, file_contents=[ImageContent(image_base64=raw)])
        ):
            if isinstance(event, TextDelta):
                full.append(event.content)
            elif isinstance(event, StreamDone):
                break
    except Exception as e:
        logging.exception("Generate flashcards error")
        raise HTTPException(502, f"Erreur IA : {e}")

    text = "".join(full).strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if "```" in text[3:] else text[3:]
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip().rstrip("`").strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise HTTPException(502, "L'IA n'a pas renvoyé un JSON valide. Réessaie avec une photo plus nette.")

    if not isinstance(data, list):
        raise HTTPException(502, "Format de réponse invalide.")

    out = []
    for item in data:
        if isinstance(item, dict) and item.get("front") and item.get("back"):
            out.append(GeneratedCard(front=str(item["front"])[:200], back=str(item["back"])[:300]))
    return out


@api_router.post("/flashcards/batch", response_model=List[Flashcard])
async def create_flashcards_batch(req: BatchCardsRequest):
    if not req.cards:
        return []
    docs = []
    for c in req.cards:
        card = Flashcard(deck=req.deck, front=c.front, back=c.back)
        docs.append(card.model_dump())
    await db.flashcards.insert_many(docs)
    return [Flashcard(**d) for d in docs]


@api_router.get("/flashcards", response_model=List[Flashcard])
async def list_flashcards(due_only: bool = False, deck: Optional[str] = None):
    q = {}
    if deck:
        q["deck"] = deck
    if due_only:
        q["next_review"] = {"$lte": now_iso()}
    docs = await db.flashcards.find(q, {"_id": 0}).sort("next_review", 1).to_list(1000)
    return [Flashcard(**d) for d in docs]


@api_router.get("/flashcards/decks")
async def list_decks():
    decks = await db.flashcards.distinct("deck")
    return {"decks": decks}


@api_router.delete("/flashcards/{card_id}")
async def delete_flashcard(card_id: str):
    r = await db.flashcards.delete_one({"id": card_id})
    return {"deleted": r.deleted_count}


@api_router.post("/flashcards/{card_id}/review", response_model=Flashcard)
async def review_flashcard(card_id: str, payload: FlashcardReview):
    doc = await db.flashcards.find_one({"id": card_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Card not found")

    q = max(0, min(5, payload.quality))
    ease = float(doc.get("ease", 2.5))
    interval = int(doc.get("interval_days", 1))

    # SM-2 light
    if q < 3:
        interval = 1
        ease = max(1.3, ease - 0.2)
    else:
        if interval == 1:
            interval = 3
        elif interval == 3:
            interval = 7
        else:
            interval = int(interval * ease)
        ease = max(1.3, ease + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02)))

    next_dt = datetime.now(timezone.utc) + timedelta(days=interval)
    doc["interval_days"] = interval
    doc["ease"] = round(ease, 2)
    doc["next_review"] = next_dt.isoformat()
    await db.flashcards.update_one({"id": card_id}, {"$set": doc})
    return Flashcard(**doc)


# ============== EMOTIONS ==============

@api_router.post("/emotions", response_model=EmotionCheckin)
async def create_emotion(data: EmotionCreate):
    e = EmotionCheckin(**data.model_dump())
    await db.emotions.insert_one(e.model_dump())
    return e


@api_router.get("/emotions", response_model=List[EmotionCheckin])
async def list_emotions(limit: int = 30):
    docs = await db.emotions.find({}, {"_id": 0}).sort("created_at", -1).to_list(limit)
    return [EmotionCheckin(**d) for d in docs]


# ============== POMODORO ==============

@api_router.post("/pomodoro", response_model=PomodoroSession)
async def create_pomodoro(data: PomodoroCreate):
    s = PomodoroSession(**data.model_dump())
    await db.pomodoros.insert_one(s.model_dump())
    return s


@api_router.get("/pomodoro", response_model=List[PomodoroSession])
async def list_pomodoros(limit: int = 50):
    docs = await db.pomodoros.find({}, {"_id": 0}).sort("created_at", -1).to_list(limit)
    return [PomodoroSession(**d) for d in docs]


# ============== WEEKLY RECAP ==============

DAY_NAMES_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]


@api_router.get("/recap/weekly")
async def weekly_recap():
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=7))
    start_iso = start.isoformat()

    # Pomodoros done this week
    pomodoros = await db.pomodoros.find(
        {"created_at": {"$gte": start_iso}, "completed": True}, {"_id": 0}
    ).to_list(1000)
    focus_minutes = sum(p.get("focus_min", 0) for p in pomodoros)

    # Tasks completed this week (use created_at fallback if no completion ts)
    tasks_done = await db.tasks.find(
        {"status": "done"}, {"_id": 0}
    ).to_list(1000)
    # Filter by created_at within week
    tasks_done = [t for t in tasks_done if (t.get("created_at") or "") >= start_iso]

    # Top subject
    by_subject = {}
    for t in tasks_done:
        s = t.get("subject") or "Autre"
        by_subject[s] = by_subject.get(s, 0) + 1
    top_subject = max(by_subject.items(), key=lambda x: x[1])[0] if by_subject else None

    # Mood
    emotions = await db.emotions.find(
        {"created_at": {"$gte": start_iso}}, {"_id": 0}
    ).to_list(1000)
    avg_mood = round(sum(e["mood"] for e in emotions) / len(emotions), 1) if emotions else None
    avg_energy = round(sum(e["energy"] for e in emotions) / len(emotions), 1) if emotions else None

    # Flashcards reviewed (heuristic: cards whose next_review changed within week)
    # We don't have a separate review log; approximate by counting cards with next_review > now (= recently reviewed)
    cards_reviewed = await db.flashcards.count_documents(
        {"next_review": {"$gte": now.isoformat()}}
    )

    # Best day: count completions per weekday
    per_day = {i: 0 for i in range(7)}
    for t in tasks_done:
        try:
            dt = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))
            per_day[dt.weekday()] += 1
        except Exception:
            pass
    for p in pomodoros:
        try:
            dt = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00"))
            per_day[dt.weekday()] += 1
        except Exception:
            pass
    best_idx = max(per_day, key=per_day.get) if any(per_day.values()) else None
    best_day = DAY_NAMES_FR[best_idx] if best_idx is not None else None

    # Estimated XP earned this week (from tracked activities)
    estimated_xp = (
        len(tasks_done) * 15
        + len(pomodoros) * 20
        + len(emotions) * 5
        + cards_reviewed * 3
    )

    # Suggested mission for next week
    if focus_minutes == 0:
        suggestion = "Lance 1 seul Pomodoro de 10 min. Juste pour goûter."
    elif focus_minutes < 30:
        suggestion = "Vise 30 min de focus total cette semaine. C'est jouable."
    elif top_subject and by_subject.get(top_subject, 0) >= 2:
        weak = next((s for s in ["Maths", "Français", "Allemand", "Anglais", "Histoire"] if s != top_subject and by_subject.get(s, 0) == 0), None)
        suggestion = f"Tu as cartonné en {top_subject}. Et si on s'attaquait à {weak or 'une autre matière'} ?" if weak else "Tu as cartonné. Garde la cadence."
    else:
        suggestion = "Cap : +1 Pomodoro et 1 devoir de plus que cette semaine."

    profile = await _get_or_create_profile()

    return {
        "week_start": start.date().isoformat(),
        "week_end": now.date().isoformat(),
        "focus_minutes": focus_minutes,
        "pomodoros_done": len(pomodoros),
        "tasks_done": len(tasks_done),
        "top_subject": top_subject,
        "cards_reviewed": cards_reviewed,
        "avg_mood": avg_mood,
        "avg_energy": avg_energy,
        "best_day": best_day,
        "estimated_xp": estimated_xp,
        "current_streak": profile.get("streak", 0),
        "current_level": profile.get("level", 1),
        "current_xp": profile.get("xp", 0),
        "suggestion": suggestion,
    }


# ============== CHAT (Claude Sonnet 4.6 streaming) ==============

@api_router.get("/chat/history/{session_id}", response_model=List[ChatMessage])
async def chat_history(session_id: str):
    docs = await db.chat_messages.find({"session_id": session_id}, {"_id": 0}).sort("created_at", 1).to_list(1000)
    return [ChatMessage(**d) for d in docs]


@api_router.delete("/chat/history/{session_id}")
async def chat_clear(session_id: str):
    r = await db.chat_messages.delete_many({"session_id": session_id})
    return {"deleted": r.deleted_count}


@api_router.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    if not EMERGENT_LLM_KEY:
        raise HTTPException(500, "EMERGENT_LLM_KEY missing")

    # Resolve image: either inline or from a task
    image_b64 = None
    if req.image_base64:
        image_b64, _mime = _normalize_image(req.image_base64)
    elif req.task_id:
        doc = await db.tasks.find_one({"id": req.task_id}, {"_id": 0, "image_base64": 1})
        if doc and doc.get("image_base64"):
            image_b64 = doc["image_base64"]

    # Save user message
    user_msg = ChatMessage(
        session_id=req.session_id,
        role="user",
        content=req.message + (" 📎" if image_b64 else ""),
    )
    await db.chat_messages.insert_one(user_msg.model_dump())

    # Load history
    history_docs = await db.chat_messages.find(
        {"session_id": req.session_id}, {"_id": 0}
    ).sort("created_at", 1).to_list(200)

    chat = LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=req.session_id,
        system_message=SYSTEM_PROMPT,
    ).with_model("anthropic", "claude-sonnet-4-6")

    # Replay history into chat (excluding the last user message we just added)
    prior = history_docs[:-1]
    for m in prior:
        if m["role"] == "user":
            async for _ in chat.stream_message(UserMessage(text=m["content"])):
                pass

    async def event_generator():
        full = []
        try:
            if image_b64:
                user_message = UserMessage(
                    text=req.message,
                    file_contents=[ImageContent(image_base64=image_b64)],
                )
            else:
                user_message = UserMessage(text=req.message)
            async for event in chat.stream_message(user_message):
                if isinstance(event, TextDelta):
                    full.append(event.content)
                    yield f"data: {json.dumps(event.content)}\n\n"
                elif isinstance(event, StreamDone):
                    break
        except Exception as e:
            logging.exception("LLM stream error")
            yield f"data: {json.dumps(f'[ERREUR] {str(e)}')}\n\n"
        finally:
            text = "".join(full).strip()
            if text:
                assistant = ChatMessage(
                    session_id=req.session_id, role="assistant", content=text
                )
                await db.chat_messages.insert_one(assistant.model_dump())
            yield "event: done\ndata: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============== THEMES ==============

@api_router.get("/themes")
async def list_themes():
    profile = await _get_or_create_profile()
    level = profile.get("level", 1)
    unlocked = set(profile.get("unlocked_themes", []))
    return [
        {
            "key": k,
            "name": meta["name"],
            "hex": meta["hex"],
            "min_level": meta["min_level"],
            "unlocked": k in unlocked or level >= meta["min_level"],
        }
        for k, meta in THEMES_META.items()
    ]


# ============== BOOSTS ==============

@api_router.get("/boosts/random")
async def random_boost():
    return random.choice(BOOSTS)


# ============== DAILY QUEST ==============

@api_router.get("/quests/today")
async def today_quest():
    today_date = datetime.now(timezone.utc).date().isoformat()
    today_start = f"{today_date}T00:00:00+00:00"

    pomodoros_today = await db.pomodoros.count_documents(
        {"completed": True, "created_at": {"$gte": today_start}}
    )
    # Approximate: count tasks done that were created today
    tasks_done_today = await db.tasks.count_documents(
        {"status": "done", "created_at": {"$gte": today_start}}
    )
    cards_count = await db.flashcards.count_documents({})
    tasks_with_image_pending = await db.tasks.count_documents(
        {"has_image": True, "analysis": None}
    )
    emotions_today = await db.emotions.count_documents(
        {"created_at": {"$gte": today_start}}
    )

    base = {"xp_reward": 25, "target": 1}

    if tasks_with_image_pending > 0:
        return {
            **base, "id": "analyze_photo",
            "title": "Analyse 1 devoir en photo",
            "desc": "Y'en a 1 qui attend. L'IA va t'aider à comprendre.",
            "cta_label": "Voir mes devoirs",
            "cta_route": "/planning",
            "done": False, "progress": 0,
        }
    if pomodoros_today == 0:
        return {
            **base, "id": "first_pomodoro",
            "title": "Lance 1 Pomodoro de 10 min",
            "desc": "Le plus dur c'est de commencer. 10 min, c'est jouable.",
            "cta_label": "Mode focus",
            "cta_route": "/pomodoro",
            "done": False, "progress": 0,
        }
    if tasks_done_today == 0:
        return {
            **base, "id": "first_task",
            "title": "Coche 1 devoir aujourd'hui",
            "desc": "Une petite tâche faite, c'est mieux qu'une grosse non finie.",
            "cta_label": "Voir mes devoirs",
            "cta_route": "/planning",
            "done": False, "progress": 0,
        }
    if cards_count < 3:
        return {
            **base, "id": "first_cards",
            "title": "Crée 3 flashcards",
            "desc": "Ou utilise la photo IA, c'est encore plus rapide.",
            "cta_label": "Aller aux Cartes",
            "cta_route": "/flashcards", "target": 3,
            "done": False, "progress": cards_count,
        }
    if emotions_today == 0:
        return {
            **base, "id": "checkin",
            "title": "Check-in émotionnel",
            "desc": "30 secondes. Tu te checkes, tu repars frais.",
            "cta_label": "Check-in",
            "cta_route": "/emotions",
            "done": False, "progress": 0,
            "xp_reward": 15,
        }
    return {
        **base, "id": "all_done",
        "title": "Mission du jour : ACCOMPLIE 🏆",
        "desc": "T'as déchiré aujourd'hui. Repos mérité.",
        "cta_label": "Bonus focus",
        "cta_route": "/pomodoro",
        "done": True, "progress": 1, "xp_reward": 0,
    }


# ============== WEEKLY QUEST (Chasse au trésor) ==============

WEEKLY_QUESTS = [
    {"id": "focus_45", "title": "45 min de focus cette semaine", "desc": "Pomodoro is the way.", "type": "focus_min", "target": 45, "reward_xp": 80, "reward_theme": None},
    {"id": "tasks_5", "title": "5 devoirs faits cette semaine", "desc": "On enchaîne les victoires.", "type": "tasks_done", "target": 5, "reward_xp": 80, "reward_theme": None},
    {"id": "cards_15", "title": "15 cartes révisées", "desc": "La mémoire, c'est ta superpuissance.", "type": "cards_reviewed", "target": 15, "reward_xp": 80, "reward_theme": None},
    {"id": "checkins_4", "title": "4 check-ins émotionnels", "desc": "Connaître son humeur = première étape.", "type": "checkins", "target": 4, "reward_xp": 60, "reward_theme": None},
    {"id": "photo_2", "title": "2 devoirs analysés en photo", "desc": "L'IA t'aide à comprendre. Profite.", "type": "photos_analyzed", "target": 2, "reward_xp": 80, "reward_theme": None},
]


@api_router.get("/quests/weekly")
async def weekly_quest():
    week_num = datetime.now(timezone.utc).isocalendar()[1]
    quest = WEEKLY_QUESTS[week_num % len(WEEKLY_QUESTS)].copy()

    # Compute progress over last 7 days
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    progress = 0

    if quest["type"] == "focus_min":
        poms = await db.pomodoros.find(
            {"completed": True, "created_at": {"$gte": week_ago}}, {"_id": 0, "focus_min": 1}
        ).to_list(1000)
        progress = sum(p.get("focus_min", 0) for p in poms)
    elif quest["type"] == "tasks_done":
        # Count tasks where status==done and created in last 7 days (approximation)
        progress = await db.tasks.count_documents(
            {"status": "done", "created_at": {"$gte": week_ago}}
        )
    elif quest["type"] == "cards_reviewed":
        # Approximation: cards with next_review pushed into the future
        now = datetime.now(timezone.utc).isoformat()
        progress = await db.flashcards.count_documents({"next_review": {"$gte": now}})
    elif quest["type"] == "checkins":
        progress = await db.emotions.count_documents({"created_at": {"$gte": week_ago}})
    elif quest["type"] == "photos_analyzed":
        progress = await db.tasks.count_documents({"has_image": True, "analysis": {"$ne": None}})

    quest["progress"] = progress
    quest["done"] = progress >= quest["target"]
    quest["week_num"] = week_num
    return quest





# ============== STATS TIMESERIES ==============

@api_router.get("/")
async def root():
    return {"message": "Coach Boost API"}


@api_router.get("/stats/timeseries")
async def stats_timeseries(days: int = 14):
    days = max(7, min(60, days))
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days - 1)
    series = []
    for i in range(days):
        d = start + timedelta(days=i)
        d_iso = d.isoformat()
        next_iso = (d + timedelta(days=1)).isoformat()
        day_start = f"{d_iso}T00:00:00+00:00"
        day_end = f"{next_iso}T00:00:00+00:00"
        rng = {"$gte": day_start, "$lt": day_end}

        poms = await db.pomodoros.find(
            {"completed": True, "created_at": rng}, {"_id": 0, "focus_min": 1}
        ).to_list(500)
        focus_min = sum(p.get("focus_min", 0) for p in poms)

        tasks_done = await db.tasks.count_documents(
            {"status": "done", "created_at": rng}
        )
        emotions = await db.emotions.find(
            {"created_at": rng}, {"_id": 0, "mood": 1, "energy": 1}
        ).to_list(500)
        avg_mood = round(sum(e["mood"] for e in emotions) / len(emotions), 1) if emotions else None
        avg_energy = round(sum(e["energy"] for e in emotions) / len(emotions), 1) if emotions else None

        series.append({
            "date": d_iso,
            "focus_min": focus_min,
            "tasks_done": tasks_done,
            "pomodoros": len(poms),
            "avg_mood": avg_mood,
            "avg_energy": avg_energy,
        })
    return {"days": days, "series": series}


# ============== PARENT SHARE (read-only) ==============

@api_router.get("/share/{token}")
async def share_view(token: str):
    p = await db.profiles.find_one({"share_token": token}, {"_id": 0, "image_base64": 0})
    if not p:
        raise HTTPException(404, "Invalid share link")

    # Aggregates over last 14 days
    end = datetime.now(timezone.utc)
    start = (end - timedelta(days=14)).isoformat()

    poms = await db.pomodoros.find(
        {"completed": True, "created_at": {"$gte": start}}, {"_id": 0, "focus_min": 1}
    ).to_list(500)
    focus_min_14d = sum(p.get("focus_min", 0) for p in poms)
    tasks_done_14d = await db.tasks.count_documents(
        {"status": "done", "created_at": {"$gte": start}}
    )
    cards_total = await db.flashcards.count_documents({})
    emotions_14d = await db.emotions.find(
        {"created_at": {"$gte": start}}, {"_id": 0, "mood": 1}
    ).to_list(500)
    avg_mood = round(sum(e["mood"] for e in emotions_14d) / len(emotions_14d), 1) if emotions_14d else None

    # Build the timeseries inline
    days = 14
    end_d = datetime.now(timezone.utc).date()
    start_d = end_d - timedelta(days=days - 1)
    series = []
    for i in range(days):
        d = start_d + timedelta(days=i)
        d_iso = d.isoformat()
        next_iso = (d + timedelta(days=1)).isoformat()
        rng = {"$gte": f"{d_iso}T00:00:00+00:00", "$lt": f"{next_iso}T00:00:00+00:00"}
        day_poms = await db.pomodoros.find(
            {"completed": True, "created_at": rng}, {"_id": 0, "focus_min": 1}
        ).to_list(500)
        day_focus = sum(p.get("focus_min", 0) for p in day_poms)
        day_tasks = await db.tasks.count_documents({"status": "done", "created_at": rng})
        series.append({"date": d_iso, "focus_min": day_focus, "tasks_done": day_tasks})

    return {
        "name": p.get("name"),
        "level": p.get("level", 1),
        "xp": p.get("xp", 0),
        "streak": p.get("streak", 0),
        "badges": p.get("badges", []),
        "stats_14d": {
            "focus_minutes": focus_min_14d,
            "tasks_done": tasks_done_14d,
            "cards_total": cards_total,
            "pomodoros_done": len(poms),
            "avg_mood": avg_mood,
            "emotions_count": len(emotions_14d),
        },
        "series": series,
    }





app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
