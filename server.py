#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unspiral — локальный прокси для живого AI-разбора (Painkiller).

Зачем: ключ LLM НЕЛЬЗЯ класть в браузерный index.html (виден в DevTools = утечка).
Сервер читает ключ из .env, браузер ходит к нему. Ключ не уходит в клиент.

Стек: только стандартная библиотека Python (http.server / urllib / json) — без pip.
Провайдер определяется по тому, какой ключ заполнен в .env (OpenAI или Gemini).

Запуск:
    cp .env.example .env        # один раз
    # вписать ключ в .env (OPENAI_API_KEY=... ИЛИ GEMINI_API_KEY=...)
    python3 server.py           # затем открыть http://localhost:8000

Без ключа сервер всё равно поднимется (отдаёт index.html), а разбор мягко
откатится на мок — в браузере будет пометка «offline preview».
"""
import json
import os
import sys
import time
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


# ─────────────────────────── .env ───────────────────────────
def load_env():
    """Простой парсер .env (без зависимостей). Не падает, если файла нет."""
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

PORT = int(os.environ.get("PORT", "8000"))
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()

# приоритет: явный PROVIDER → иначе тот ключ, что заполнен
PROVIDER = os.environ.get("PROVIDER", "").strip().lower()
if not PROVIDER:
    PROVIDER = "openai" if OPENAI_KEY else ("gemini" if GEMINI_KEY else "none")

# ─────────── защита публичного эндпоинта от абьюза (URL может утечь) ───────────
# Лимиты настраиваются через .env. Превышение → запрос мягко падает на мок в браузере.
RATE_PER_MIN = int(os.environ.get("RATE_PER_MIN", "15"))     # запросов/мин с одного IP
DAILY_CAP = int(os.environ.get("DAILY_CAP", "800"))          # суммарный потолок запросов/сутки
MAX_ANSWER_CHARS = int(os.environ.get("MAX_ANSWER_CHARS", "2000"))  # обрезка ввода (потолок токенов)

_lock = threading.Lock()
_hits = {}                      # ip -> [timestamps за последнюю минуту]
_day = {"date": None, "count": 0}


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def check_limits(ip):
    """Возвращает (ok: bool, reason: str). Потокобезопасно."""
    now = time.time()
    with _lock:
        # суточный потолок
        if _day["date"] != _today():
            _day["date"], _day["count"] = _today(), 0
        if _day["count"] >= DAILY_CAP:
            return False, "daily_cap"
        # rate per IP за 60 сек
        window = [t for t in _hits.get(ip, []) if now - t < 60]
        if len(window) >= RATE_PER_MIN:
            _hits[ip] = window
            return False, "rate_limited"
        window.append(now)
        _hits[ip] = window
        _day["count"] += 1
        return True, ""


# ─────────────────────── промпт разбора ───────────────────────
THEME_CTX = {
    "relationships": "recurring toxic patterns in their relationships",
    "self_sabotage": "self-sabotage — wanting something then undoing it",
    "people_pleasing": "people-pleasing — saying yes when they mean no",
    "anxiety": "anxiety and overthinking — looping thoughts",
}
# Калибровка тона из reflection-quality-rubric.md (раздел C). Ядро обоих: честность + забота.
TONE_VOICE = {
    "gentle": ("Gentle — truth with warmth. Name what actually hurts first, then hold it with care; "
               "never bury it under positivity or empty praise. Validate the feeling, then SHIFT — "
               "always add one concrete next step or question. Soft is not lowering the bar."),
    "direct": ("Direct — honest with respect. Go after the pattern, never the person; no shame, no labels "
               "like 'lazy' or 'broken'. Be candid and unsparing but CALM — never angry or cruel "
               "(brutal is not required to be honest). Say 'it seems…' not an absolute verdict, then give "
               "a clear direction. One sharp challenge at a time."),
}


def build_messages(body):
    theme = body.get("theme", "relationships")
    tone = body.get("tone", "gentle")
    answer = (body.get("answer") or "").strip()
    deeper = bool(body.get("deeper"))
    prev_line = (body.get("prevLine") or "").strip()
    prev_q = (body.get("prevQ") or "").strip()

    # Системный промпт собран по research/reflection-quality-rubric.md (главный риск ниши —
    # разбор должен быть глубоким/личным, не generic).
    system = (
        "You are Unspiral, a guide for shadow-work self-reflection journaling. "
        "This is wellness and self-reflection, NOT therapy: never use the words "
        "'therapy', 'heal trauma', 'diagnose', never give medical/clinical claims, and never "
        "tell the user what they 'should' feel.\n"
        f"The user's theme: {THEME_CTX.get(theme, theme)}.\n"
        f"Voice: {TONE_VOICE.get(tone, TONE_VOICE['gentle'])}\n\n"
        "Read what they wrote and reply with two parts:\n"
        "  1) 'line' — a 2-4 sentence reflection. This is the 'aha' moment.\n"
        "  2) 'question' — exactly ONE question that goes one level deeper.\n\n"
        "Make it land (do):\n"
        "- Name the UNSAID: voice the pattern or fear they implied but didn't state — read between "
        "the lines; do NOT just paraphrase their words back.\n"
        "- Anchor to THEIR specifics: reflect their own words and details, not general theory; quote "
        "a phrase of theirs when natural.\n"
        "- Move from 'what' to 'why': advance the causality one step; never just log the feeling.\n"
        "- Reframe the shadow as a wounded part that wants to be seen, not a defect to fix.\n"
        "- Challenge, don't flatter: offer the uncomfortable truth or an alternative angle. "
        "Default affirmation reads as generic and hollow.\n"
        "- The question must be answerable only by THIS person and pull toward the root/origin.\n\n"
        "Avoid:\n"
        "- No neutral, averaged, advice-column tone — it feels almost meaningless.\n"
        "- Don't read mood from keywords ('excited/proud' does not mean they're fine); watch for the "
        "contradiction between their words and what's underneath.\n"
        "- No cliches, no woo-woo/pseudoscience dressed as fact, no preaching, no infantilizing an adult.\n"
        "- Generic test: if the reflection could fit a random stranger, rewrite it to their specifics.\n"
        "- Use the audience's own words where natural: spiraling, going in circles, triggers, glimmers, "
        "patterns, calling me out, do the work.\n"
        "- Keep it tight — it must fit a small phone screen without scrolling.\n\n"
        "SAFETY: if the writing shows crisis, self-harm, severe dissociation, or being so overwhelmed "
        "they can't think/function, do NOT probe deeper. Set 'safety' true, gently land them (a breath, "
        "the present moment, permission to stop), and point to support — in the US they can call or text "
        "988, and naming a trusted person helps. Otherwise 'safety' is false.\n"
        'Return ONLY strict JSON: {"line": "...", "question": "...", "safety": false}.'
    )
    if deeper:
        system += (
            "\nThis is the SECOND, deeper pass. Build on your previous reflection "
            f'("{prev_line}") and previous question ("{prev_q}"). Go toward the root / origin '
            "of the pattern — older and more honest — without repeating your earlier phrasing."
        )

    user = f"Prompt they answered: about {THEME_CTX.get(theme, theme)}.\nWhat they wrote:\n{answer}"
    return system, user


# ─────────────────────── вызовы провайдеров ───────────────────────
def _post_json(url, payload, headers, timeout=30):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_json(text):
    """Достаёт {line,question,safety} из ответа модели (на случай обёрток)."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        i, j = text.find("{"), text.rfind("}")
        if i != -1 and j != -1 and j > i:
            return json.loads(text[i:j + 1])
        raise


def call_openai(system, user, deeper):
    payload = {
        "model": OPENAI_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.8 if deeper else 0.95,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    data = _post_json("https://api.openai.com/v1/chat/completions", payload, headers)
    return _extract_json(data["choices"][0]["message"]["content"])


def call_gemini(system, user, deeper):
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}")
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": 0.8 if deeper else 0.95,
            "responseMimeType": "application/json",
        },
    }
    data = _post_json(url, payload, {"Content-Type": "application/json"})
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return _extract_json(text)


def reflect(body):
    """Возвращает (dict_or_None, error_str_or_None)."""
    if PROVIDER == "none":
        return None, "no_api_key"
    system, user = build_messages(body)
    deeper = bool(body.get("deeper"))
    try:
        out = call_openai(system, user, deeper) if PROVIDER == "openai" else call_gemini(system, user, deeper)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        print(f"[reflect] HTTP {e.code} от {PROVIDER}: {detail}", file=sys.stderr)
        return None, f"{PROVIDER}_http_{e.code}"
    except Exception as e:
        print(f"[reflect] ошибка {PROVIDER}: {e}", file=sys.stderr)
        return None, str(e)

    line = (out.get("line") or "").strip()
    question = (out.get("question") or "").strip()
    if not line:
        return None, "empty_line"
    return {"line": line, "question": question, "safety": bool(out.get("safety"))}, None


# ─────────────────────── аналитика воронки ───────────────────────
# Логируем события визита в events.jsonl. /stats показывает: кто дошёл, кто дал фидбэк, что хорошо/плохо.
EVENTS_FILE = os.path.join(HERE, "events.jsonl")
STATS_TOKEN = os.environ.get("STATS_TOKEN", "").strip()   # если задан — /stats?token=... обязателен
_evlock = threading.Lock()
_ALLOWED_EVENT_KEYS = {"t", "sid", "ts", "screen", "theme", "tone", "kind", "label",
                       "plan", "got", "tone_pref", "text", "chosen_tone", "h"}


def append_event(ip, payload):
    if not isinstance(payload, dict):
        return
    rec = {k: payload[k] for k in _ALLOWED_EVENT_KEYS if k in payload}
    rec["ip"] = ip
    rec["srv_ts"] = int(time.time())
    if isinstance(rec.get("text"), str):
        rec["text"] = rec["text"][:300]
    line = json.dumps(rec, ensure_ascii=False)[:1000]
    with _evlock:
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    # фидбэк дублируем в stdout — виден в логах хоста, даже если инстанс пересоздан и файл сброшен
    if rec.get("t") in ("feedback", "trial_click"):
        print("[event]", line, flush=True)


def compute_stats():
    if not os.path.exists(EVENTS_FILE):
        return {"events": 0}
    seen_screen = {}     # sid -> set(screens)
    sids = set()
    sid_hyp = {}         # sid -> гипотеза спринта (spiral/map/honest), из ?h= в ссылке
    trial = set()
    go_deeper, tone_switch, fb_skip = set(), 0, 0
    got = {"yes": 0, "kinda": 0, "no": 0}
    tone_pref = {"gentle": 0, "direct": 0}
    comments, total = [], 0
    with _evlock:
        rows = open(EVENTS_FILE, encoding="utf-8").read().splitlines()
    for ln in rows:
        try:
            e = json.loads(ln)
        except Exception:
            continue
        total += 1
        sid = e.get("sid", "?")
        sids.add(sid)
        h = e.get("h")
        if h and sid not in sid_hyp:
            sid_hyp[sid] = h
        t = e.get("t")
        if t == "screen":
            seen_screen.setdefault(sid, set()).add(e.get("screen"))
        elif t == "go_deeper":
            go_deeper.add(sid)
        elif t == "trial_click":
            trial.add(sid)
        elif t == "tone_switch":
            tone_switch += 1
        elif t == "feedback_skip":
            fb_skip += 1
        elif t == "feedback":
            if e.get("got") in got:
                got[e["got"]] += 1
            if e.get("tone_pref") in tone_pref:
                tone_pref[e["tone_pref"]] += 1
            txt = (e.get("text") or "").strip()
            if txt:
                comments.append({"text": txt, "got": e.get("got"), "tone": e.get("tone_pref")})

    def reached(sc):
        return sum(1 for s in seen_screen.values() if sc in s)

    visits = len(sids)

    # Разрез воронки по гипотезам спринта (какой крючок гонит трафик до «ага»). Считаем фиксированные 3 + «без метки».
    by_hyp = {}
    for hyp in ("spiral", "map", "honest", "bio", "(none)"):
        members = [sid for sid in sids if sid_hyp.get(sid, "(none)") == hyp]
        by_hyp[hyp] = {
            "visits": len(members),
            "reflection": sum(1 for sid in members if "s_reflection" in seen_screen.get(sid, set())),
            "paywall": sum(1 for sid in members if "paywall" in seen_screen.get(sid, set())),
            "trial": sum(1 for sid in members if sid in trial),
            "done": sum(1 for sid in members if "done" in seen_screen.get(sid, set())),
        }

    return {
        "events": total, "visits": visits, "by_hyp": by_hyp,
        "reached_session": reached("s_answer"),
        "reached_reflection": reached("s_reflection"),
        "reached_paywall": reached("paywall"),
        "trial_clicked": len(trial),
        "reached_feedback": reached("feedback"),
        "completed": reached("done"),
        "go_deeper": len(go_deeper),
        "tone_switch": tone_switch,
        "got": got, "tone_pref": tone_pref,
        "feedback_skip": fb_skip,
        "comments": comments[-40:],
    }


def render_stats_html(s):
    if not s.get("events"):
        return "<html><body style='font-family:system-ui;background:#0c0a14;color:#eee;padding:40px'>" \
               "<h2>Unspiral — статистика</h2><p>Событий пока нет. Прогоните прототип через публичную ссылку.</p></body></html>"
    v = max(s["visits"], 1)

    def pct(n):
        return f"{round(100 * n / v)}%"

    def bar(label, n):
        return (f"<tr><td style='padding:6px 12px'>{label}</td>"
                f"<td style='padding:6px 12px;text-align:right'>{n}</td>"
                f"<td style='padding:6px 12px;color:#f3c79b'>{pct(n)}</td></tr>")

    funnel = "".join([
        bar("Визиты (старт)", s["visits"]),
        bar("Дошли до сессии (написали ответ)", s["reached_session"]),
        bar("Дошли до разбора (момент «ага»)", s["reached_reflection"]),
        bar("Жали «Go deeper»", s["go_deeper"]),
        bar("Дошли до paywall", s["reached_paywall"]),
        bar("Жали «Start trial»", s["trial_clicked"]),
        bar("Дошли до фидбэка", s["reached_feedback"]),
        bar("Завершили (до конца)", s["completed"]),
    ])
    # Разрез по гипотезам: рендерим только фиксированные крючки + «без метки», если по нему был трафик.
    bh = s.get("by_hyp", {})
    HYP_LABELS = {"spiral": "🌀 SPIRAL (h1 «иду по кругу»)", "map": "🗺️ MAP (h2 «бросили без ответа»)",
                  "honest": "🔥 HONEST (h3 выбор тона)", "bio": "🔗 BIO (профиль)",
                  "(none)": "— без метки (прямой заход)"}

    def hyp_row(key):
        d = bh.get(key) or {}
        vis = d.get("visits", 0)
        if key in ("bio", "(none)") and vis == 0:
            return ""
        refl = d.get("reflection", 0)
        aha = f"{round(100 * refl / vis)}%" if vis else "—"
        cells = "".join(f"<td style='padding:6px 12px;text-align:right'>{x}</td>"
                        for x in (vis, f"{refl} <span style='color:#f3c79b'>{aha}</span>",
                                  d.get("paywall", 0), d.get("trial", 0), d.get("done", 0)))
        return f"<tr><td style='padding:6px 12px'>{HYP_LABELS[key]}</td>{cells}</tr>"

    hyp_rows = "".join(hyp_row(k) for k in ("spiral", "map", "honest", "bio", "(none)"))
    hyp_head = ("<tr style='color:#888'>" + "".join(
        f"<td style='padding:6px 12px{'' if i == 0 else ';text-align:right'}'>{h}</td>"
        for i, h in enumerate(("Гипотеза", "Визиты", "→ Разбор (ага)", "→ Paywall", "→ Trial", "→ Конец"))) + "</tr>")
    hyp_table = ("<h3 style='margin-top:26px'>По гипотезам — крючок → «ага» <span style='font-size:12px;color:#888'>"
                 "(метка из ?h= в ссылке)</span></h3>"
                 "<table style='border-collapse:collapse;width:100%;background:#15121f;border-radius:10px;font-size:14px'>"
                 f"{hyp_head}{hyp_rows}</table>")

    g = s["got"]
    tp = s["tone_pref"]
    comments = "".join(
        f"<li style='margin:8px 0'><span style='color:#f3c79b'>[{c.get('got') or '—'} · {c.get('tone') or '—'}]</span> "
        f"{(c['text'])}</li>" for c in reversed(s["comments"])
    ) or "<li style='color:#888'>пока нет текстовых комментариев</li>"

    return f"""<html><head><meta charset='utf-8'><meta http-equiv='refresh' content='20'>
<title>Unspiral stats</title></head>
<body style='font-family:system-ui;background:#0c0a14;color:#eee;padding:28px;max-width:680px;margin:auto'>
<h2 style='font-weight:600'>Unspiral — воронка теста <span style='font-size:12px;color:#888'>(автообновление 20с)</span></h2>
<table style='border-collapse:collapse;width:100%;background:#15121f;border-radius:10px'>{funnel}</table>
{hyp_table}
<h3 style='margin-top:26px'>Разбор «попал»?</h3>
<p>👍 Yes: <b>{g['yes']}</b> &nbsp; 😐 Kind of: <b>{g['kinda']}</b> &nbsp; 👎 Not really: <b>{g['no']}</b></p>
<h3>Какой тон сильнее (A/B)</h3>
<p>🌙 Gentle: <b>{tp['gentle']}</b> &nbsp; 🔥 Direct: <b>{tp['direct']}</b> &nbsp;
<span style='color:#888'>· live-переключений тона: {s['tone_switch']} · пропустили фидбэк: {s['feedback_skip']}</span></p>
<h3>Что писали (последние)</h3>
<ul style='line-height:1.4;font-size:14px'>{comments}</ul>
<p style='color:#666;font-size:12px;margin-top:24px'>events.jsonl · всего событий: {s['events']}</p>
</body></html>"""


# ─────────────────────── HTTP-сервер ───────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # тише в консоли
        pass

    def _send(self, code, ctype, body_bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _json(self, code, obj):
        self._send(code, "application/json; charset=utf-8", json.dumps(obj).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            fp = os.path.join(HERE, "index.html")
            try:
                with open(fp, "rb") as f:
                    self._send(200, "text/html; charset=utf-8", f.read())
            except FileNotFoundError:
                self._send(404, "text/plain; charset=utf-8", b"index.html not found")
        elif path == "/stats":
            # опциональная защита токеном (?token=...), если STATS_TOKEN задан в .env
            if STATS_TOKEN:
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                token = dict(p.split("=", 1) for p in qs.split("&") if "=" in p).get("token", "")
                if token != STATS_TOKEN:
                    self._send(401, "text/plain; charset=utf-8", "нужен ?token=...".encode("utf-8"))
                    return
            html = render_stats_html(compute_stats())
            self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def _client_ip(self):
        # за cloudflared/реверс-прокси реальный IP — в этих заголовках
        fwd = self.headers.get("Cf-Connecting-Ip") or self.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "unknown"

    def _read_body(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > 16000:   # потолок тела запроса
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_POST(self):
        path = self.path.split("?")[0]

        # аналитика воронки — дёшево, без LLM, мягкие лимиты
        if path == "/api/event":
            try:
                payload = self._read_body()
            except Exception:
                payload = {}
            append_event(self._client_ip(), payload)
            self._send(204, "text/plain", b"")
            return

        if path != "/api/reflect":
            self._json(404, {"error": "not_found"})
            return

        # защита от абьюза публичного URL (LLM стоит денег)
        ok, reason = check_limits(self._client_ip())
        if not ok:
            self._json(200, {"error": reason})   # фронт мягко покажет мок
            return
        try:
            body = self._read_body()
        except Exception as e:
            self._json(400, {"error": f"bad_request: {e}"})
            return
        body["answer"] = (body.get("answer") or "")[:MAX_ANSWER_CHARS]   # потолок токенов
        result, err = reflect(body)
        if err:
            # фронт мягко откатится на мок
            self._json(200, {"error": err})
            return
        self._json(200, result)


def main():
    print("─" * 60)
    if PROVIDER == "none":
        print("⚠️  Ключ не найден в .env (OPENAI_API_KEY или GEMINI_API_KEY).")
        print("   Сервер поднимется, но разбор будет МОКом (offline preview).")
        print("   Чтобы включить живой разбор: cp .env.example .env и впиши ключ.")
    else:
        model = OPENAI_MODEL if PROVIDER == "openai" else GEMINI_MODEL
        print(f"✅ Провайдер: {PROVIDER}  ·  модель: {model}  (ключ читается из .env, в клиент не уходит)")
    print(f"🛡  Лимиты: {RATE_PER_MIN}/мин с IP · {DAILY_CAP}/сутки всего · ввод ≤{MAX_ANSWER_CHARS} симв.")
    print(f"▶  Прототип:  http://localhost:{PORT}")
    print(f"📊 Статистика: http://localhost:{PORT}/stats" + ("  (нужен ?token=...)" if STATS_TOKEN else ""))
    print("   (Ctrl+C — остановить)")
    print("─" * 60)
    # 0.0.0.0 — чтобы работало и локально (туннель), и на хосте (Render даёт свой $PORT)
    host = os.environ.get("HOST", "0.0.0.0")
    try:
        ThreadingHTTPServer((host, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")


if __name__ == "__main__":
    main()
