import os, json, re, random, string, hashlib, asyncio, threading
import html as _html
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List, Tuple

import telebot
from telebot import types
import aiohttp
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CODE = os.getenv("ADMIN_CODE", "admin123")
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "")
DATA_FILE = "bot_data.json"
YANDEX_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
bot = telebot.TeleBot(BOT_TOKEN)

user_states: Dict[str, Dict[str, Any]] = {}

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def gen_id(prefix: str, n: int = 8) -> str:
    return prefix + "".join(random.choices(string.digits, k=n))

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

MSK_TZ = timezone(timedelta(hours=3))

def now_msk() -> datetime:
    return datetime.now(MSK_TZ)

def dt_from_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None

def fmt_dt_msk(dt: Optional[datetime]) -> str:
    if not dt:
        return "-"
    return dt.astimezone(MSK_TZ).strftime("%d.%m.%Y %H:%M МСК")

def parse_date_token(s: str) -> Optional[datetime.date]:
    t = (s or "").strip().lower()
    if t in ("сегодня", "today"):
        return now_msk().date()
    if t in ("завтра", "tomorrow"):
        return (now_msk() + timedelta(days=1)).date()
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", t)
    if not m:
        return None
    d, mo, y = map(int, m.groups())
    try:
        return datetime(y, mo, d).date()
    except Exception:
        return None

def parse_time_token(s: str) -> Optional[Tuple[int, int]]:
    m = re.match(r"^(\d{1,2}):(\d{2})$", (s or "").strip())
    if not m:
        return None
    hh, mm = map(int, m.groups())
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm

def combine_date_time(d: datetime.date, hm: Tuple[int, int]) -> datetime:
    hh, mm = hm
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=MSK_TZ)

def assignment_window_status(a: Dict[str, Any], now: datetime) -> Tuple[str, Optional[datetime], Optional[datetime]]:
    open_at = dt_from_iso(a.get("open_at"))
    due_at = dt_from_iso(a.get("due_at"))
    if open_at and now < open_at:
        return "not_open", open_at, due_at
    if due_at and now > due_at:
        return "closed", open_at, due_at
    return "open", open_at, due_at

def assignment_status_label(a: Dict[str, Any], now: datetime) -> str:
    status, open_at, due_at = assignment_window_status(a, now)
    if status == "not_open":
        return f"⏳ с {fmt_dt_msk(open_at)}"
    if status == "closed":
        return f"⌛ до {fmt_dt_msk(due_at)}"
    if due_at:
        return f"до {fmt_dt_msk(due_at)}"
    return "без дедлайна"

def assignment_can_submit(a: Dict[str, Any], now: datetime) -> Tuple[bool, str]:
    status, open_at, due_at = assignment_window_status(a, now)
    if status == "not_open":
        return False, f"Задание доступно с {fmt_dt_msk(open_at)}."
    if status == "closed":
        return False, f"Срок сдачи истёк ({fmt_dt_msk(due_at)})."
    return True, ""

def find_invite(data: Dict[str, Any], code: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    for cid, c in data.get("classes", {}).items():
        if not isinstance(c, dict):
            continue
        invs = c.get("invites", {})
        if not isinstance(invs, dict):
            continue
        inv = invs.get(code)
        if isinstance(inv, dict):
            return cid, inv
    return None, None

def invite_valid(inv: Dict[str, Any], now: datetime) -> Tuple[bool, str]:
    exp = dt_from_iso(inv.get("expires_at"))
    if exp and now > exp:
        return False, "Инвайт истёк."
    max_uses = int(inv.get("max_uses", 1))
    uses = int(inv.get("uses", 0))
    if uses >= max_uses:
        return False, "Инвайт уже использован."
    return True, ""

def format_ok(text: str, regex: Optional[str]) -> bool:
    if not regex:
        return True
    try:
        return re.fullmatch(regex, (text or "").strip()) is not None
    except re.error:
        return True

def get_class_students(data: Dict[str, Any], class_id: str) -> List[Tuple[str, Dict[str, Any]]]:
    return [(sid, u) for sid, u in data.get("users", {}).items()
            if isinstance(u, dict) and u.get("role") == "student" and u.get("class_id") == class_id]

def has_result(data: Dict[str, Any], assignment_id: str, student_id: str) -> bool:
    for r in data.get("results", {}).values():
        if not isinstance(r, dict):
            continue
        if r.get("assignment_id") == assignment_id and r.get("student_id") == student_id:
            return True
    return False

def send_code_block(chat_id: int, code: str, reply_markup=None) -> None:
    """Send code safely using Telegram HTML <pre><code> to avoid Markdown entity errors."""
    snippet = code if len(code) < 3500 else code[:3500] + "\n... (обрезано)"
    esc = _html.escape(snippet)
    bot.send_message(chat_id, f"<pre><code>{esc}</code></pre>", parse_mode="HTML", reply_markup=reply_markup)


def extract_json_obj(txt: str) -> Optional[Dict[str, Any]]:
    if not txt: 
        return None
    a, b = txt.find("{"), txt.rfind("}") + 1
    if a < 0 or b <= 0:
        return None
    try:
        obj = json.loads(txt[a:b])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None

def ctf_fingerprint(kind: str, subtype: str, challenge: str, instruction: str, teacher_guide: str, expected_hash: str) -> str:
    raw = "|".join([
        norm(kind), norm(subtype),
        norm(challenge)[:5000],
        norm(instruction)[:2000],
        norm(teacher_guide)[:5000],
        expected_hash or ""
    ])
    return sha(raw)

def seen_fingerprint(data: Dict[str,Any], fp: str) -> bool:
    arr = data.get("ctf_fingerprints")
    return isinstance(arr, list) and fp in arr

def add_fingerprint(data: Dict[str,Any], fp: str) -> None:
    if not isinstance(data.get("ctf_fingerprints"), list):
        data["ctf_fingerprints"] = []
    # ограничим рост списка
    data["ctf_fingerprints"].append(fp)
    if len(data["ctf_fingerprints"]) > 3000:
        data["ctf_fingerprints"] = data["ctf_fingerprints"][-2000:]

def flag_once_ok(text: str) -> bool:
    if not text: 
        return False
    # ищем именно lapin{...}
    flags = re.findall(r"lapin\{[^\}]{3,64}\}", text)
    return len(flags) == 1

async def gen_crypto_bundle_yagpt(topic_or_text: str, has_text: bool, flag: str, subtype: str, params: Dict[str,Any], nonce: str) -> Optional[Dict[str, str]]:
    """Возвращает dict: title, plaintext, student_hint, teacher_guide (всё уникально)."""
    mode = "ДАН ТЕКСТ УЧИТЕЛЯ" if has_text else "СГЕНЕРИРУЙ ТЕКСТ"
    # Требуем JSON без лишнего текста
    prompt = f"""{mode}.
Нужно подготовить CTF задание по кибербезопасности/криптографии.
Тип: {subtype}
Параметры (если есть): {json.dumps(params, ensure_ascii=False)}
Nonce для уникальности: {nonce}

Данные:
- Тема/текст: {topic_or_text}
- Флаг (вставь РОВНО 1 раз): {flag}

Сгенерируй:
1) title — короткий заголовок (уникальный)
2) plaintext — связный текст (3-7 предложений) с флагом внутри ровно 1 раз (lapin{{...}}), без списков
   - если дан текст учителя: можешь слегка перефразировать/добавить 1-2 предложения, но смысл сохраняй
3) student_hint — подсказка ученику (1-2 предложения) как решать (без «взлома», только учебная крипто-логика)
4) teacher_guide — пошаговое решение для учителя (5-9 шагов, нумерованный список), как получить ответ из задания.
   - решение должно быть именно под этот тип ({subtype}) и параметры
   - НЕ используй вредоносные payload'ы/эксплойт-инструкции, только учебное расшифрование/декодирование

Формат ответа: строго JSON:
{{"title":str,"plaintext":str,"student_hint":str,"teacher_guide":str}}
Без текста вне JSON."""
    txt = await yandex_completion(prompt, temperature=0.55, max_tokens=1200)
    obj = extract_json_obj(txt or "")
    if not obj: 
        return None
    for k in ("title","plaintext","student_hint","teacher_guide"):
        if not isinstance(obj.get(k), str) or not obj[k].strip():
            return None
    return {k: obj[k].strip() for k in ("title","plaintext","student_hint","teacher_guide")}

async def gen_web_bundle_yagpt(vuln_label: str, embedded_flag: str, expected_answer: str, nonce: str) -> Optional[Dict[str, str]]:
    """Возвращает dict: title, description, student_instruction, code, teacher_guide (всё уникально)."""
    prompt = f"""Сгенерируй уникальное WEB CTF задание в формате code review (анализ кода, без эксплуатации).
Тема уязвимости: {vuln_label}
Nonce для уникальности: {nonce}

Требования:
- Дай небольшой фрагмент кода (до ~80 строк) на Python/HTML/JS (любой, но читаемый).
- В коде должен быть встроен флаг РОВНО 1 раз: {embedded_flag}
  (например, переменная FLAG или HTML-комментарий).
- «Ожидаемый ответ» для бота: {expected_answer}
  (если он совпадает с флагом — отлично; если нет — сделай так, чтобы ответ можно было понять по коду, не пряча его очевидной строкой "answer = ...")
- Уязвимость должна быть понятной по коду (например: небезопасный хеш пароля / SQL-конкатенация / XSS из-за отсутствия экранирования).
- Никаких пошаговых payload'ов, SQL-инъекционных строк, эксплуатационных инструкций. Только анализ кода.

Сгенерируй:
1) title — уникальный заголовок
2) description — описание задания для ученика (2-4 предложения)
3) student_instruction — что сделать ученику (1-2 предложения)
4) code — сам код (строкой, без Markdown)
5) teacher_guide — пошаговое решение для учителя (5-9 шагов), как по коду прийти к ожидаемому ответу.

Формат: строго JSON:
{{"title":str,"description":str,"student_instruction":str,"code":str,"teacher_guide":str}}
Без текста вне JSON."""
    txt = await yandex_completion(prompt, temperature=0.65, max_tokens=1600)
    obj = extract_json_obj(txt or "")
    if not obj: 
        return None
    for k in ("title","description","student_instruction","code","teacher_guide"):
        if not isinstance(obj.get(k), str) or not obj[k].strip():
            return None
    return {k: obj[k].strip() for k in ("title","description","student_instruction","code","teacher_guide")}


def ensure(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict): data = {}
    for k in ["users","classes","tests","ctf_tasks","homeworks","assignments","results"]:
        data.setdefault(k, {})
    data.setdefault("ctf_fingerprints", [])
    # migrate old tests with class_id into assignments
    try:
        pairs = {(a.get("class_id"), a.get("ref_id")) for a in data["assignments"].values() if isinstance(a, dict) and a.get("kind")=="test"}
        for t in data["tests"].values():
            if not isinstance(t, dict): continue
            cid, tid = t.get("class_id"), t.get("id")
            if cid and tid and (cid, tid) not in pairs:
                aid = gen_id("A")
                data["assignments"][aid] = {"id":aid,"class_id":cid,"teacher_id":t.get("teacher_id"),"kind":"test","ref_id":tid,"title":f"Тест: {t.get('topic','')}", "created_at": now_iso()}
                pairs.add((cid, tid))
    except Exception:
        pass
    return data

def load_data() -> Dict[str, Any]:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return ensure(json.load(f))
        except Exception:
            return ensure({})
    return ensure({})

def save_data(data: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(ensure(data), f, ensure_ascii=False, indent=2)

def run_async(coro):
    def runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(coro)
    threading.Thread(target=runner, daemon=True).start()

def kb_teacher():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("🔐 Инвайт в класс","🔒 Приватность класса")
    kb.add("✅ Создать класс","🧑‍🏫 Ваши классы")
    kb.add("🧪 Создать задание","📚 Ваши тесты")
    kb.add("🏁 Ваши CTF","📊 Результаты")
    kb.add("ℹ️ Помощь")
    return kb

def kb_student():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("📚 Мои задания","📈 Мои результаты")
    kb.add("ℹ️ Помощь")
    return kb

def kb_cancel():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("❌ Отмена")
    return kb

def need_reg(u: Dict[str, Any]) -> bool:
    if not u or u.get("role") not in ("teacher","student"): return True
    p = u.get("profile") or {}
    for k in ["last_name","first_name","age","email"]:
        if not str(p.get(k,"")).strip(): return True
    return False

def role_choice(chat_id: int, name: str):
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton("👨‍🏫 Создатель", callback_data="role_teacher"),
           types.InlineKeyboardButton("🎓 Обучающийся", callback_data="role_student"))
    bot.send_message(chat_id, f"Привет, {name}! Выберите роль:", reply_markup=mk)

async def yandex_completion(prompt: str, temperature: float = 0.3, max_tokens: int = 1000) -> Optional[str]:
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        return None
    headers = {"Authorization": f"Api-Key {YANDEX_API_KEY}", "x-folder-id": YANDEX_FOLDER_ID, "Content-Type": "application/json"}
    payload = {
        "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/latest",
        "completionOptions": {"stream": False, "temperature": temperature, "maxTokens": max_tokens},
        "messages": [{"role":"system","text":"Ты преподаватель кибербезопасности. Не давай инструкций по взлому."},
                     {"role":"user","text": prompt}],
    }
    async with aiohttp.ClientSession() as s:
        try:
            async with s.post(YANDEX_URL, headers=headers, json=payload, timeout=40) as r:
                if r.status != 200: return None
                j = await r.json()
                alts = j.get("result", {}).get("alternatives", [])
                if not alts: return None
                return alts[0].get("message", {}).get("text", "")
        except Exception:
            return None

async def gen_test(topic: str, n: int, diff: str) -> Optional[List[Dict[str, Any]]]:
    prompt = f"""Сгенерируй тест по кибербезопасности на тему "{topic}".
Вопросов: {n}. Сложность: {diff}.
Безопасность: без пошагового взлома/эксплуатации.
Формат: строго JSON {{\"questions\":[{{\"question\":str,\"options\":[4 str],\"correct\":0..3,\"explanation\":str}}...]}} без текста вне JSON."""
    txt = await yandex_completion(prompt, 0.3, 1400)
    if not txt: return None
    a, b = txt.find("{"), txt.rfind("}")+1
    if a<0 or b<=0: return None
    try:
        obj = json.loads(txt[a:b])
        qs = []
        for q in obj.get("questions", []):
            if not isinstance(q, dict): continue
            if not (isinstance(q.get("question"), str) and isinstance(q.get("options"), list) and isinstance(q.get("correct"), int)): 
                continue
            if len(q["options"])!=4 or not (0<=q["correct"]<=3): 
                continue
            qs.append({"question": q["question"].strip(), "options": [str(x) for x in q["options"]], "correct": q["correct"], "explanation": str(q.get("explanation",""))})
        return qs[:n] if qs else None
    except Exception:
        return None

def gen_flag() -> str:
    return "lapin{" + "".join(random.choices(string.ascii_lowercase+string.digits, k=12)) + "}"

def caesar(s: str, shift: int) -> str:
    abc = string.ascii_lowercase + string.ascii_uppercase + string.digits
    out=[]
    for ch in s:
        if ch in abc:
            out.append(abc[(abc.index(ch)+shift)%len(abc)])
        else:
            out.append(ch)
    return "".join(out)

def vigenere(s: str, key: str) -> str:
    key = re.sub(r"[^a-z]", "", (key or "").lower()) or "key"
    out=[]; j=0
    for ch in s:
        if ch.isalpha():
            k = ord(key[j%len(key)])-97
            base = 97 if ch.islower() else 65
            out.append(chr((ord(ch)-base+k)%26+base)); j+=1
        else:
            out.append(ch)
    return "".join(out)

def xor_hex(s: str, key: str) -> str:
    kb = (key or "k").encode()
    b = s.encode()
    return bytes([b[i]^kb[i%len(kb)] for i in range(len(b))]).hex()

def obfuscate2(s: str) -> Tuple[str,str]:
    noise = string.ascii_letters+string.digits
    out=[]
    for ch in s:
        out.append(ch); out.append(random.choice(noise))
    return "".join(out), "Удалите каждый 2-й символ, начиная со 2-го."

def base64_noise(s: str) -> Tuple[str,str]:
    import base64
    b64 = base64.b64encode(s.encode()).decode()
    noise = string.ascii_letters+string.digits
    out=[]
    for i,ch in enumerate(b64):
        out.append(ch)
        if (i+1)%5==0: out.append(random.choice(noise))
    return "".join(out), "Удалите каждый 6-й символ (шум), затем декодируйте Base64."


def build_teacher_guide_crypto(title: str, subtype: str, hint: str, meta: Dict[str,Any], expected_flag: str) -> str:
    steps = []
    steps.append("👨‍🏫 Инструкция для учителя (решение):")
    steps.append(f"Задание: {title} (crypto/{subtype})")
    steps.append("")
    # subtype-specific
    if subtype == "obf":
        steps += [
            "1) Примените правило деобфускации: удалите каждый 2-й символ, начиная со 2-го (это убирает «шум»).",
            "2) В восстановленном тексте найдите подстроку вида lapin{...}.",
            "3) Убедитесь, что отправляете флаг целиком, включая скобки { }."
        ]
    elif subtype == "caesar":
        sh = meta.get("shift")
        steps += [
            f"1) Это шифр Caesar. Сдвиг указан в условии: {sh}.",
            "2) Для расшифровки сдвигайте символы в обратную сторону (на -shift).",
            "3) В расшифрованном тексте найдите lapin{...} и отправьте флаг."
        ]
    elif subtype == "vig":
        key = meta.get("key")
        steps += [
            f"1) Это Vigenère. Ключ указан в условии: {key}.",
            "2) Для расшифровки примените обратный сдвиг для каждой буквы по ключу (вычитание вместо сложения).",
            "3) В результате найдите lapin{...} и отправьте флаг."
        ]
    elif subtype == "xor":
        key = meta.get("key")
        steps += [
            f"1) Это XOR-hex. Ключ указан в условии: {key}.",
            "2) Преобразуйте hex-строку в байты.",
            "3) Сделайте XOR каждого байта с байтом ключа (ключ повторяется по кругу).",
            "4) Преобразуйте байты обратно в текст, найдите lapin{...} и отправьте флаг."
        ]
    elif subtype == "b64":
        steps += [
            "1) В строку вставлен «шум». Удалите каждый 6-й символ (шум).",
            "2) Оставшуюся строку декодируйте из Base64.",
            "3) В полученном тексте найдите lapin{...} и отправьте флаг."
        ]
    else:
        steps += [
            "1) Внимательно прочитайте подсказку из задания и выполните обратные шаги.",
            "2) Найдите lapin{...} и отправьте флаг."
        ]

    steps.append("")
    steps.append(f"✅ Ожидаемый ответ (для учителя): {expected_flag}")
    steps.append("")
    steps.append("Примечание: эта инструкция предназначена для проверки/подсказок ученикам, а не для автоматического «взлома» реальных систем.")
    return "\n".join(steps)


def build_teacher_guide_web(title: str, subtype: str, expected: str, embedded_flag: str) -> str:
    steps = []
    steps.append("👨‍🏫 Инструкция для учителя (решение):")
    steps.append(f"Задание: {title} (web/{subtype})")
    steps.append("")
    # Web задания у нас формата code review: решение через анализ кода, без эксплуатации
    if subtype == "insecure":
        steps += [
            "1) Посмотрите на функцию хранения пароля: используется md5/sha1/без соли — это небезопасно.",
            "2) Проверьте, где в коде спрятан флаг (обычно это переменная FLAG).",
            "3) Ответом является флаг/строка, которую нужно отправить боту."
        ]
    elif subtype == "sqli":
        steps += [
            "1) Найдите место, где SQL-запрос строится конкатенацией строк с пользовательским вводом — это риск SQLi.",
            "2) Флаг встроен прямо в пример (переменная FLAG).",
            "3) Ответом является флаг/строка, которую нужно отправить боту."
        ]
    elif subtype == "xss":
        steps += [
            "1) Найдите небезопасный вывод пользовательского ввода в HTML без экранирования — это риск XSS.",
            "2) Флаг спрятан в шаблоне (например, в HTML-комментарии).",
            "3) Ответом является флаг/строка, которую нужно отправить боту."
        ]
    else:
        steps += [
            "1) Найдите уязвимый паттерн в коде по теме задания.",
            "2) Найдите, где спрятан флаг/ответ, и отправьте его."
        ]

    steps.append("")
    steps.append(f"✅ Ожидаемый ответ (для учителя): {expected}")
    if expected != embedded_flag:
        steps.append(f"(В код при этом встроен флаг для ориентирования: {embedded_flag})")
    steps.append("")
    steps.append("Примечание: формат задания — code review, без пошаговой эксплуатации и без вредоносных payload'ов.")
    return "\n".join(steps)


WEB = {
 "insecure": ("Web: Небезопасный хеш пароля","Найдите проблему хранения пароля и флаг в коде.",
              lambda flag: f"""import hashlib
def store_password(p): return hashlib.md5(p.encode()).hexdigest()  # плохо
FLAG = \"{flag}\""""),
 "sqli": ("Web: SQLi риск (code review)","Найдите опасную конкатенацию и флаг в коде.",
          lambda flag: f"""def get_user(db,u):
    q = \"SELECT * FROM users WHERE username = '\" + u + \"';\"
    return db.execute(q)
FLAG = \"{flag}\""""),
 "xss": ("Web: XSS риск (code review)","Найдите небезопасный вывод и флаг в шаблоне.",
         lambda flag: f"""<div>Hi, {{user_input}}!</div>
<!-- {flag} -->"""),
}

# --------------- START / REG ---------------

@bot.message_handler(commands=["start"])
def start(message):
    data = load_data()
    uid = str(message.from_user.id)
    u = data["users"].get(uid)
    if not u or need_reg(u):
        role_choice(message.chat.id, message.from_user.first_name or "друг")
        return
    bot.send_message(message.chat.id, "Меню.", reply_markup=kb_teacher() if u["role"]=="teacher" else kb_student())

@bot.callback_query_handler(func=lambda c: c.data in ("role_teacher","role_student"))
def cb_role(c):
    uid = str(c.from_user.id)
    role = "teacher" if c.data=="role_teacher" else "student"
    user_states[uid] = {"flow":"reg","step":"last","role":role,"profile":{}}
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id, "Регистрация: фамилия?", reply_markup=kb_cancel())

@bot.message_handler(func=lambda m: user_states.get(str(m.from_user.id),{}).get("flow")=="reg")
def reg(m):
    uid = str(m.from_user.id)
    st = user_states[uid]
    if m.text=="❌ Отмена":
        user_states.pop(uid,None)
        role_choice(m.chat.id, m.from_user.first_name or "друг")
        return
    t=(m.text or "").strip()
    p=st["profile"]
    if st["step"]=="last":
        p["last_name"]=t; st["step"]="first"; bot.reply_to(m,"Имя?"); return
    if st["step"]=="first":
        p["first_name"]=t; st["step"]="mid"; bot.reply_to(m,"Отчество (или '-')?"); return
    if st["step"]=="mid":
        p["middle_name"]="" if t=="-" else t; st["step"]="age"; bot.reply_to(m,"Возраст (число)?"); return
    if st["step"]=="age":
        if not t.isdigit(): bot.reply_to(m,"Возраст числом."); return
        p["age"]=int(t); st["step"]="email"; bot.reply_to(m,"Email?"); return
    if st["step"]=="email":
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", t): bot.reply_to(m,"Похоже не email, попробуйте ещё."); return
        p["email"]=t
        data=load_data()
        u=data["users"].get(uid,{})
        u["role"]=st["role"]; u["profile"]=p; u["username"]=m.from_user.first_name or "Пользователь"
        data["users"][uid]=u; save_data(data)
        st["step"]="admin" if u["role"]=="teacher" else "class_code"
        bot.send_message(m.chat.id, "Код учителя?" if u["role"]=="teacher" else "Код класса?", reply_markup=kb_cancel()); return
    if st["step"]=="admin":
        if t!=ADMIN_CODE: bot.reply_to(m,"Неверно. Попробуйте снова."); return
        user_states.pop(uid,None)
        bot.send_message(m.chat.id,"✅ Вы учитель.", reply_markup=kb_teacher()); return
    if st["step"]=="class_code":
        data=load_data()
        now = now_msk()
        cid, inv = find_invite(data, t)
        if cid and inv:
            ok, msg = invite_valid(inv, now)
            if not ok:
                bot.reply_to(m, msg)
                return
            inv["uses"] = int(inv.get("uses", 0)) + 1
            inv.setdefault("used_by", []).append(uid)
            inv["last_used_at"] = now_iso()
            data["users"][uid]["class_id"] = cid
            save_data(data)
            user_states.pop(uid,None)
            bot.send_message(m.chat.id,"✅ Вы в классе по инвайту.", reply_markup=kb_student()); return
        cid=None
        for k,v in data["classes"].items():
            if isinstance(v,dict) and v.get("access_code")==t:
                if v.get("private"):
                    bot.reply_to(m,"Класс приватный. Нужен инвайт.")
                    return
                cid=k
                break
        if not cid: bot.reply_to(m,"Класс не найден. Попробуйте снова."); return
        data["users"][uid]["class_id"]=cid; save_data(data)
        user_states.pop(uid,None)
        bot.send_message(m.chat.id,"✅ Вы в классе.", reply_markup=kb_student()); return

# --------------- TEACHER: CLASSES ---------------

@bot.message_handler(func=lambda m: m.text=="✅ Создать класс")
def t_create_class(m):
    data=load_data(); uid=str(m.from_user.id)
    if data["users"].get(uid,{}).get("role")!="teacher": bot.reply_to(m,"Только учителю."); return
    user_states[uid]={"flow":"class","step":"name"}
    bot.send_message(m.chat.id,"Название класса?", reply_markup=kb_cancel())

@bot.message_handler(func=lambda m: user_states.get(str(m.from_user.id),{}).get("flow")=="class")
def t_create_class_flow(m):
    uid=str(m.from_user.id)
    if m.text=="❌ Отмена":
        user_states.pop(uid,None); bot.send_message(m.chat.id,"Отменено.", reply_markup=kb_teacher()); return
    name=(m.text or "").strip()
    if len(name)<2: bot.reply_to(m,"Коротко. Ещё раз."); return
    data=load_data()
    cid=gen_id("CL"); code="".join(random.choices(string.ascii_uppercase+string.digits,k=6))
    data["classes"][cid]={"id":cid,"name":name,"teacher_id":uid,"access_code":code,"created_at":now_iso()}
    save_data(data); user_states.pop(uid,None)
    safe_name = _html.escape(name)
    bot.send_message(m.chat.id, f"✅ Класс создан: {safe_name}\nКод: <code>{code}</code>", parse_mode="HTML", reply_markup=kb_teacher())

@bot.message_handler(func=lambda m: m.text=="🧑‍🏫 Ваши классы")
def t_classes(m):
    data=load_data(); uid=str(m.from_user.id)
    if data["users"].get(uid,{}).get("role")!="teacher": bot.reply_to(m,"Только учителю."); return
    cls=[c for c in data["classes"].values() if isinstance(c,dict) and c.get("teacher_id")==uid]
    if not cls: bot.send_message(m.chat.id,"Классов нет.", reply_markup=kb_teacher()); return
    out=["🧑‍🏫 Ваши классы:"]
    for c in cls:
        studs=[u for u in data["users"].values() if isinstance(u,dict) and u.get("role")=="student" and u.get("class_id")==c.get("id")]
        out.append(f"• {c.get('name')} — код {c.get('access_code')} — учеников {len(studs)}")
    bot.send_message(m.chat.id,"\n".join(out), reply_markup=kb_teacher())

# --------------- TEACHER: CLASS INVITES / PRIVACY ---------------

@bot.message_handler(func=lambda m: m.text=="🔐 Инвайт в класс")
def t_class_invite(m):
    data=load_data(); uid=str(m.from_user.id)
    if data["users"].get(uid,{}).get("role")!="teacher":
        bot.reply_to(m,"Только учителю.")
        return
    cls=[c for c in data["classes"].values() if isinstance(c,dict) and c.get("teacher_id")==uid]
    if not cls:
        bot.send_message(m.chat.id,"Классов нет.", reply_markup=kb_teacher())
        return
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for c in cls:
        kb.add(f"Класс: {c['name']}")
    kb.add("❌ Отмена")
    user_states[uid]={"flow":"class_invite","step":"class"}
    bot.send_message(m.chat.id,"Выберите класс:", reply_markup=kb)

@bot.message_handler(func=lambda m: user_states.get(str(m.from_user.id),{}).get("flow")=="class_invite")
def t_class_invite_flow(m):
    uid=str(m.from_user.id); st=user_states[uid]
    if m.text=="❌ Отмена":
        user_states.pop(uid,None); bot.send_message(m.chat.id,"Ок.", reply_markup=kb_teacher()); return
    t=(m.text or "").strip()
    data=load_data()
    if st["step"]=="class":
        if not t.startswith("Класс: "):
            bot.reply_to(m,"Кнопкой.")
            return
        name=t.replace("Класс: ","",1).strip()
        cid=None
        for k,v in data["classes"].items():
            if isinstance(v,dict) and v.get("teacher_id")==uid and v.get("name")==name:
                cid=k
                break
        if not cid:
            bot.reply_to(m,"Класс не найден.")
            return
        st["cid"]=cid; st["step"]="exp_date"
        bot.send_message(m.chat.id,"Дата окончания инвайта (ДД.ММ.ГГГГ или 'сегодня/завтра'):", reply_markup=kb_cancel())
        return
    if st["step"]=="exp_date":
        if t=="❌ Отмена":
            user_states.pop(uid,None); bot.send_message(m.chat.id,"Ок.", reply_markup=kb_teacher()); return
        d=parse_date_token(t)
        if not d:
            bot.reply_to(m,"Нужна дата: ДД.ММ.ГГГГ или 'сегодня/завтра'.")
            return
        st["exp_date"]=d; st["step"]="exp_time"
        bot.send_message(m.chat.id,"Время (ЧЧ:ММ, МСК):", reply_markup=kb_cancel())
        return
    if st["step"]=="exp_time":
        if t=="❌ Отмена":
            user_states.pop(uid,None); bot.send_message(m.chat.id,"Ок.", reply_markup=kb_teacher()); return
        hm=parse_time_token(t)
        if not hm:
            bot.reply_to(m,"Время в формате ЧЧ:ММ.")
            return
        exp_dt=combine_date_time(st["exp_date"], hm)
        if exp_dt <= now_msk():
            bot.reply_to(m,"Время уже прошло. Укажите будущее.")
            return
        st["exp_dt"]=exp_dt; st["step"]="max_uses"
        bot.send_message(m.chat.id,"Сколько использований? (1-100, по умолчанию 1):", reply_markup=kb_cancel())
        return
    if st["step"]=="max_uses":
        if t=="❌ Отмена":
            user_states.pop(uid,None); bot.send_message(m.chat.id,"Ок.", reply_markup=kb_teacher()); return
        max_uses = 1
        if t and t.isdigit():
            max_uses = int(t)
        if max_uses < 1 or max_uses > 100:
            bot.reply_to(m,"1-100.")
            return
        cid=st["cid"]
        c=data["classes"].get(cid)
        if not c:
            user_states.pop(uid,None); bot.send_message(m.chat.id,"Класс не найден.", reply_markup=kb_teacher()); return
        c.setdefault("invites", {})
        code="".join(random.choices(string.ascii_uppercase+string.digits, k=8))
        while code in c["invites"]:
            code="".join(random.choices(string.ascii_uppercase+string.digits, k=8))
        c["invites"][code]={
            "code": code,
            "expires_at": st["exp_dt"].isoformat(),
            "max_uses": max_uses,
            "uses": 0,
            "created_at": now_iso()
        }
        save_data(data)
        user_states.pop(uid,None)
        bot.send_message(
            m.chat.id,
            f"✅ Инвайт создан:\nКод: <code>{code}</code>\nДействует до: {fmt_dt_msk(st['exp_dt'])}",
            parse_mode="HTML",
            reply_markup=kb_teacher()
        )
        return

@bot.message_handler(func=lambda m: m.text=="🔒 Приватность класса")
def t_class_privacy(m):
    data=load_data(); uid=str(m.from_user.id)
    if data["users"].get(uid,{}).get("role")!="teacher":
        bot.reply_to(m,"Только учителю.")
        return
    cls=[c for c in data["classes"].values() if isinstance(c,dict) and c.get("teacher_id")==uid]
    if not cls:
        bot.send_message(m.chat.id,"Классов нет.", reply_markup=kb_teacher())
        return
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for c in cls:
        kb.add(f"Класс: {c['name']}")
    kb.add("❌ Отмена")
    user_states[uid]={"flow":"class_privacy","step":"class"}
    bot.send_message(m.chat.id,"Выберите класс:", reply_markup=kb)

@bot.message_handler(func=lambda m: user_states.get(str(m.from_user.id),{}).get("flow")=="class_privacy")
def t_class_privacy_flow(m):
    uid=str(m.from_user.id); st=user_states[uid]
    if m.text=="❌ Отмена":
        user_states.pop(uid,None); bot.send_message(m.chat.id,"Ок.", reply_markup=kb_teacher()); return
    t=(m.text or "").strip()
    data=load_data()
    if st["step"]=="class":
        if not t.startswith("Класс: "):
            bot.reply_to(m,"Кнопкой.")
            return
        name=t.replace("Класс: ","",1).strip()
        cid=None
        for k,v in data["classes"].items():
            if isinstance(v,dict) and v.get("teacher_id")==uid and v.get("name")==name:
                cid=k
                break
        if not cid:
            bot.reply_to(m,"Класс не найден.")
            return
        st["cid"]=cid; st["step"]="toggle"
        c=data["classes"][cid]
        status="приватный" if c.get("private") else "публичный"
        kb=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.add("🔒 Включить", "🔓 Выключить")
        kb.add("❌ Отмена")
        bot.send_message(m.chat.id,f"Статус: {status}. Изменить?", reply_markup=kb)
        return
    if st["step"]=="toggle":
        cid=st["cid"]
        c=data["classes"].get(cid)
        if not c:
            user_states.pop(uid,None); bot.send_message(m.chat.id,"Класс не найден.", reply_markup=kb_teacher()); return
        if t=="🔒 Включить":
            c["private"]=True
        elif t=="🔓 Выключить":
            c["private"]=False
        else:
            bot.reply_to(m,"Кнопкой.")
            return
        save_data(data)
        user_states.pop(uid,None)
        bot.send_message(m.chat.id,"✅ Обновлено.", reply_markup=kb_teacher())
        return

# --------------- TEACHER: CREATE TASK ---------------

@bot.message_handler(func=lambda m: m.text=="🧪 Создать задание")
def t_create_task(m):
    data=load_data(); uid=str(m.from_user.id)
    if data["users"].get(uid,{}).get("role")!="teacher": bot.reply_to(m,"Только учителю."); return
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, row_width=2)
    kb.add("📝 Создать тест","🏁 Создать CTF"); kb.add("❌ Отмена")
    user_states[uid]={"flow":"task","step":"pick"}
    bot.send_message(m.chat.id,"Тип задания?", reply_markup=kb)

@bot.message_handler(func=lambda m: user_states.get(str(m.from_user.id),{}).get("flow")=="task")
def t_create_task_flow(m):
    uid=str(m.from_user.id)
    if m.text=="❌ Отмена":
        user_states.pop(uid,None); bot.send_message(m.chat.id,"Ок.", reply_markup=kb_teacher()); return
    if m.text=="📝 Создать тест":
        user_states[uid]={"flow":"test_create","step":"topic"}
        bot.send_message(m.chat.id,"Тема теста?", reply_markup=kb_cancel()); return
    if m.text=="🏁 Создать CTF":
        kb=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, row_width=2)
        kb.add("🔐 Crypto","🌐 Web"); kb.add("❌ Отмена")
        user_states[uid]={"flow":"ctf_create","step":"kind"}
        bot.send_message(m.chat.id,"CTF направление?", reply_markup=kb); return
    bot.reply_to(m,"Выберите кнопкой.")

# --------------- TEACHER: TEST CREATE ---------------

@bot.message_handler(func=lambda m: user_states.get(str(m.from_user.id),{}).get("flow")=="test_create")
def t_test_create(m):
    uid=str(m.from_user.id); st=user_states[uid]
    if m.text=="❌ Отмена":
        user_states.pop(uid,None); bot.send_message(m.chat.id,"Отменено.", reply_markup=kb_teacher()); return
    t=(m.text or "").strip()
    if st["step"]=="topic":
        st["topic"]=t; st["step"]="num"; bot.send_message(m.chat.id,"Сколько вопросов (3-30)?", reply_markup=kb_cancel()); return
    if st["step"]=="num":
        if not t.isdigit(): bot.reply_to(m,"Число."); return
        n=int(t)
        if n<3 or n>30: bot.reply_to(m,"3..30"); return
        st["n"]=n; st["step"]="diff"
        kb=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, row_width=3)
        kb.add("Лёгкая","Средняя","Сложная"); kb.add("❌ Отмена")
        bot.send_message(m.chat.id,"Сложность?", reply_markup=kb); return
    if st["step"]=="diff":
        mp={"Лёгкая":"easy","Средняя":"medium","Сложная":"hard"}
        if t not in mp: bot.reply_to(m,"Выберите кнопкой."); return
        bot.send_message(m.chat.id,"Генерирую…", reply_markup=types.ReplyKeyboardRemove())
        run_async(finalize_test(uid, st["topic"], st["n"], mp[t], m.chat.id))
        user_states.pop(uid,None); return

async def finalize_test(teacher_id: str, topic: str, n: int, diff: str, chat_id: int):
    qs = await gen_test(topic, n, diff)
    if not qs:
        bot.send_message(chat_id,"❌ Не удалось сгенерировать тест (проверьте Yandex ключи).", reply_markup=kb_teacher()); return
    data=load_data()
    tid=gen_id("T")
    data["tests"][tid]={"id":tid,"teacher_id":teacher_id,"topic":topic,"difficulty":diff,"questions":qs,"created_at":now_iso()}
    save_data(data)
    mk=types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton("📌 Назначить в класс", callback_data=f"assign_test:{tid}"),
           types.InlineKeyboardButton("Позже", callback_data="assign_later"))
    bot.send_message(chat_id,f"✅ Тест создан: {topic}\nID: {tid}\nВопросов: {len(qs)}", reply_markup=mk)

# --------------- TEACHER: HOMEWORK CREATE ---------------

@bot.message_handler(func=lambda m: user_states.get(str(m.from_user.id),{}).get("flow")=="hw_create")
def t_hw_create(m):
    uid=str(m.from_user.id); st=user_states[uid]
    if m.text=="❌ Отмена":
        user_states.pop(uid,None); bot.send_message(m.chat.id,"Отменено.", reply_markup=kb_teacher()); return
    t=(m.text or "").strip()
    if st["step"]=="title":
        if len(t) < 3:
            bot.reply_to(m,"Коротко. Ещё раз.")
            return
        st["title"]=t; st["step"]="text"
        bot.send_message(m.chat.id,"Текст задания/инструкция:", reply_markup=kb_cancel()); return
    if st["step"]=="text":
        if len(t) < 3:
            bot.reply_to(m,"Добавьте описание.")
            return
        st["text"]=t; st["step"]="format"
        bot.send_message(m.chat.id,"Формат ответа (regex) или '-' чтобы без проверки:", reply_markup=kb_cancel()); return
    if st["step"]=="format":
        st["format_regex"]=None if t=="-" else t
        st["step"]="open_date"
        bot.send_message(m.chat.id,"Когда открыть? (сейчас или дата ДД.ММ.ГГГГ):", reply_markup=kb_cancel()); return
    if st["step"]=="open_date":
        if t.lower()=="сейчас":
            st["open_at"]=None
            st["step"]="due_date"
            bot.send_message(m.chat.id,"Дедлайн: дата ДД.ММ.ГГГГ или '-' без дедлайна:", reply_markup=kb_cancel()); return
        d=parse_date_token(t)
        if not d:
            bot.reply_to(m,"Нужна дата: ДД.ММ.ГГГГ или 'сегодня/завтра', либо 'сейчас'.")
            return
        st["open_date"]=d; st["step"]="open_time"
        bot.send_message(m.chat.id,"Время открытия (ЧЧ:ММ, МСК):", reply_markup=kb_cancel()); return
    if st["step"]=="open_time":
        hm=parse_time_token(t)
        if not hm:
            bot.reply_to(m,"Время в формате ЧЧ:ММ.")
            return
        open_at=combine_date_time(st["open_date"], hm)
        if open_at <= now_msk():
            bot.reply_to(m,"Время уже прошло. Укажите будущее.")
            return
        st["open_at"]=open_at
        st["step"]="due_date"
        bot.send_message(m.chat.id,"Дедлайн: дата ДД.ММ.ГГГГ или '-' без дедлайна:", reply_markup=kb_cancel()); return
    if st["step"]=="due_date":
        if t=="-":
            st["due_at"]=None
        else:
            d=parse_date_token(t)
            if not d:
                bot.reply_to(m,"Нужна дата: ДД.ММ.ГГГГ или '-'.")
                return
            st["due_date"]=d; st["step"]="due_time"
            bot.send_message(m.chat.id,"Время дедлайна (ЧЧ:ММ, МСК):", reply_markup=kb_cancel()); return
        # create homework
        data=load_data()
        hid=gen_id("H")
        data["homeworks"][hid]={
            "id":hid,
            "teacher_id":uid,
            "title":st["title"],
            "text":st["text"],
            "format_regex":st.get("format_regex"),
            "open_at": st["open_at"].isoformat() if st.get("open_at") else None,
            "due_at": st.get("due_at").isoformat() if st.get("due_at") else None,
            "created_at": now_iso()
        }
        save_data(data)
        user_states.pop(uid,None)
        mk=types.InlineKeyboardMarkup()
        mk.add(types.InlineKeyboardButton("📌 Назначить в класс", callback_data=f"assign_hw:{hid}"),
               types.InlineKeyboardButton("Позже", callback_data="assign_later"))
        bot.send_message(m.chat.id,f"✅ Домашнее задание создано: {st['title']}\nID: {hid}", reply_markup=mk)
        return
    if st["step"]=="due_time":
        hm=parse_time_token(t)
        if not hm:
            bot.reply_to(m,"Время в формате ЧЧ:ММ.")
            return
        due_at=combine_date_time(st["due_date"], hm)
        open_at=st.get("open_at")
        if open_at and due_at <= open_at:
            bot.reply_to(m,"Дедлайн должен быть позже открытия.")
            return
        if due_at <= now_msk():
            bot.reply_to(m,"Дедлайн уже прошёл. Укажите будущее.")
            return
        st["due_at"]=due_at
        data=load_data()
        hid=gen_id("H")
        data["homeworks"][hid]={
            "id":hid,
            "teacher_id":uid,
            "title":st["title"],
            "text":st["text"],
            "format_regex":st.get("format_regex"),
            "open_at": st["open_at"].isoformat() if st.get("open_at") else None,
            "due_at": due_at.isoformat(),
            "created_at": now_iso()
        }
        save_data(data)
        user_states.pop(uid,None)
        mk=types.InlineKeyboardMarkup()
        mk.add(types.InlineKeyboardButton("📌 Назначить в класс", callback_data=f"assign_hw:{hid}"),
               types.InlineKeyboardButton("Позже", callback_data="assign_later"))
        bot.send_message(m.chat.id,f"✅ Домашнее задание создано: {st['title']}\nID: {hid}", reply_markup=mk)
        return

# --------------- TEACHER: CTF CREATE ---------------

@bot.message_handler(func=lambda m: user_states.get(str(m.from_user.id),{}).get("flow")=="ctf_create")
def t_ctf_create(m):
    uid=str(m.from_user.id); st=user_states[uid]
    if m.text=="❌ Отмена":
        user_states.pop(uid,None); bot.send_message(m.chat.id,"Отменено.", reply_markup=kb_teacher()); return
    t=(m.text or "").strip()
    if st["step"]=="kind":
        if t=="🔐 Crypto":
            st["kind"]="crypto"; st["step"]="crypto_type"
            kb=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, row_width=2)
            kb.add("Обфускация","Caesar"); kb.add("Vigenère","XOR-hex"); kb.add("Base64+шум"); kb.add("❌ Отмена")
            bot.send_message(m.chat.id,"Тип crypto?", reply_markup=kb); return
        if t=="🌐 Web":
            st["kind"]="web"; st["step"]="web_type"
            kb=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, row_width=2)
            kb.add("Небезопасный хеш пароля","SQLi (code review)"); kb.add("XSS (code review)"); kb.add("❌ Отмена")
            bot.send_message(m.chat.id,"Тип web?", reply_markup=kb); return
        bot.reply_to(m,"Выберите кнопкой."); return

    if st["step"]=="crypto_type":
        mp={"Обфускация":"obf","Caesar":"caesar","Vigenère":"vig","XOR-hex":"xor","Base64+шум":"b64"}
        if t not in mp: bot.reply_to(m,"Выберите кнопкой."); return
        st["sub"]=mp[t]; st["step"]="crypto_text_q"
        kb=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, row_width=2)
        kb.add("Да","Нет"); kb.add("❌ Отмена")
        bot.send_message(m.chat.id,"Есть свой текст?", reply_markup=kb); return

    if st["step"]=="crypto_text_q":
        if t not in ("Да","Нет"): bot.reply_to(m,"Да/Нет."); return
        st["has_text"]=(t=="Да"); st["step"]="crypto_text" if st["has_text"] else "crypto_topic"
        bot.send_message(m.chat.id, "Отправьте текст:" if st["has_text"] else "Тема для генерации текста?", reply_markup=kb_cancel()); return

    if st["step"] in ("crypto_text","crypto_topic"):
        st["val"]=t
        bot.send_message(m.chat.id,"Создаю CTF…", reply_markup=types.ReplyKeyboardRemove())
        run_async(finalize_crypto(uid, st, m.chat.id))
        user_states.pop(uid,None); return

    if st["step"]=="web_type":
        sub=None
        if t=="Небезопасный хеш пароля": sub="insecure"
        elif t=="SQLi (code review)": sub="sqli"
        elif t=="XSS (code review)": sub="xss"
        else: bot.reply_to(m,"Выберите кнопкой."); return
        st["sub"]=sub; st["flag"]=gen_flag(); st["step"]="web_expected"
        bot.send_message(m.chat.id, f"Введите ожидаемый ответ (или '-' чтобы оставить флаг):\n`{st['flag']}`", parse_mode="Markdown", reply_markup=kb_cancel()); return

    if st["step"]=="web_expected":
        expected = st["flag"] if t=="-" else t
        st["expected"]=expected
        bot.send_message(m.chat.id,"Создаю web CTF…", reply_markup=types.ReplyKeyboardRemove())
        run_async(finalize_web(uid, st, m.chat.id))
        user_states.pop(uid,None); return

async def finalize_crypto(teacher_id: str, st: Dict[str,Any], chat_id: int):
    # Все crypto CTF генерируем через YandexGPT, чтобы были уникальны и с уникальным объяснением.
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        bot.send_message(chat_id,"❌ Не настроены ключи YandexGPT (.env).", reply_markup=kb_teacher())
        return

    data = load_data()
    sub = st["sub"]

    # случайные параметры (чтобы задачи отличались)
    meta: Dict[str,Any] = {"max_attempts": 5}
    if sub == "caesar":
        meta["shift"] = random.randint(3, 20)
    elif sub == "vig":
        meta["key"] = "".join(random.choices(string.ascii_lowercase, k=6))
    elif sub == "xor":
        meta["key"] = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    elif sub == "b64":
        meta["rule"] = "remove_every_6th"
    else:
        meta["rule"] = "remove_every_2nd"

    # Генерация бандла (title/plaintext/hint/teacher_guide) с проверкой уникальности
    attempts = 0
    bundle = None
    flag = gen_flag()

    while attempts < 4:
        attempts += 1
        nonce = gen_id("N", 10)
        bundle = await gen_crypto_bundle_yagpt(
            topic_or_text=st["val"],
            has_text=bool(st.get("has_text")),
            flag=flag,
            subtype=sub,
            params=meta,
            nonce=nonce
        )
        if not bundle:
            continue
        plaintext = bundle["plaintext"]

        # проверка: флаг один раз
        if not flag_once_ok(plaintext):
            bundle = None
            continue

        # локально шифруем (чтобы проверка ответа была стабильной)
        if sub == "obf":
            chall, auto_hint = obfuscate2(plaintext)
        elif sub == "caesar":
            chall = caesar(plaintext, int(meta["shift"]))
            auto_hint = f"Caesar, сдвиг {meta['shift']}."
        elif sub == "vig":
            chall = vigenere(plaintext, str(meta["key"]))
            auto_hint = f"Vigenère, ключ {meta['key']}."
        elif sub == "xor":
            chall = xor_hex(plaintext, str(meta["key"]))
            auto_hint = f"XOR-hex, ключ {meta['key']}."
        else:
            chall, auto_hint = base64_noise(plaintext)

        # student_hint из YandexGPT (уникальный); но если пустой — fallback на авто-подсказку
        student_hint = bundle.get("student_hint") or auto_hint
        teacher_guide = bundle.get("teacher_guide") or ""

        expected_hash = sha(norm(flag))
        fp = ctf_fingerprint("crypto", sub, chall, student_hint, teacher_guide, expected_hash)

        if seen_fingerprint(data, fp):
            bundle = None
            continue

        # сохраним fingerprint и выйдем
        add_fingerprint(data, fp)
        break

    if not bundle:
        bot.send_message(chat_id,"❌ Не удалось сгенерировать уникальное CTF через YandexGPT (попробуйте ещё раз).", reply_markup=kb_teacher())
        return

    # Пересобираем challenge ещё раз (после выхода из цикла у нас уже есть plaintext/hint)
    plaintext = bundle["plaintext"]
    if sub == "obf":
        chall, _ = obfuscate2(plaintext)
        title = bundle["title"] or "Crypto: Обфускация"
    elif sub == "caesar":
        chall = caesar(plaintext, int(meta["shift"]))
        title = bundle["title"] or "Crypto: Caesar"
    elif sub == "vig":
        chall = vigenere(plaintext, str(meta["key"]))
        title = bundle["title"] or "Crypto: Vigenère"
    elif sub == "xor":
        chall = xor_hex(plaintext, str(meta["key"]))
        title = bundle["title"] or "Crypto: XOR-hex"
    else:
        chall, _ = base64_noise(plaintext)
        title = bundle["title"] or "Crypto: Base64+шум"

    hint = bundle["student_hint"]
    teacher_guide = bundle["teacher_guide"]

    tid = gen_id("C")
    data["ctf_tasks"][tid] = {
        "id": tid,
        "teacher_id": teacher_id,
        "kind": "crypto",
        "subtype": sub,
        "title": title,
        "description": "Найдите флаг lapin{...} в восстановленном тексте.",
        "challenge": chall,
        "instruction": hint,
        "expected_plain": flag,
        "expected_hash": sha(norm(flag)),
        "teacher_guide": teacher_guide,
        "meta": meta,
        "created_at": now_iso()
    }
    save_data(data)

    mk = types.InlineKeyboardMarkup()
    mk.add(
        types.InlineKeyboardButton("📌 Назначить в класс", callback_data=f"assign_ctf:{tid}"),
        types.InlineKeyboardButton("Позже", callback_data="assign_later")
    )

    # учителю: уникальное решение + ожидаемый ответ
    bot.send_message(chat_id, f"✅ CTF создан: {title}\nID: {tid}\n\n{teacher_guide}\n\n✅ Ожидаемый ответ: {flag}", reply_markup=types.ReplyKeyboardRemove())

    bot.send_message(chat_id, f"📌 Вариант задания для ученика:\nПодсказка: {hint}")
    send_code_block(chat_id, chall, reply_markup=mk)

async def finalize_web(teacher_id: str, st: Dict[str,Any], chat_id: int):
    # Все web CTF генерируем через YandexGPT, чтобы были уникальны и с уникальным объяснением.
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        bot.send_message(chat_id,"❌ Не настроены ключи YandexGPT (.env).", reply_markup=kb_teacher())
        return

    data = load_data()
    vuln_label = st["sub"]  # мы храним как "insecure"/"sqli"/"xss" сейчас; передадим как есть + человекочит.
    embedded_flag = st["flag"]
    expected = st["expected"]

    # добавим немного человекочитаемости
    label_map = {
        "insecure": "Небезопасный хеш пароля",
        "sqli": "SQLi (code review)",
        "xss": "XSS (code review)"
    }
    vuln_human = label_map.get(vuln_label, vuln_label)

    attempts = 0
    bundle = None
    while attempts < 4:
        attempts += 1
        nonce = gen_id("N", 10)
        bundle = await gen_web_bundle_yagpt(
            vuln_label=vuln_human,
            embedded_flag=embedded_flag,
            expected_answer=expected,
            nonce=nonce
        )
        if not bundle:
            continue

        code = bundle["code"]
        # проверка: флаг один раз
        if len(re.findall(r"lapin\{[^\}]{3,64}\}", code)) != 1:
            bundle = None
            continue

        expected_hash = sha(norm(expected))
        fp = ctf_fingerprint("web", vuln_label, code, bundle["student_instruction"], bundle["teacher_guide"], expected_hash)
        if seen_fingerprint(data, fp):
            bundle = None
            continue
        add_fingerprint(data, fp)
        break

    if not bundle:
        bot.send_message(chat_id,"❌ Не удалось сгенерировать уникальное Web CTF через YandexGPT (попробуйте ещё раз).", reply_markup=kb_teacher())
        return

    tid = gen_id("W")
    data["ctf_tasks"][tid] = {
        "id": tid,
        "teacher_id": teacher_id,
        "kind": "web",
        "subtype": vuln_label,
        "title": bundle["title"],
        "description": bundle["description"],
        "challenge": bundle["code"],
        "instruction": bundle["student_instruction"],
        "expected_hash": sha(norm(expected)),
        "embedded_flag": embedded_flag,
        "expected_plain": expected,
        "teacher_guide": bundle["teacher_guide"],
        "created_at": now_iso()
    }
    save_data(data)

    mk = types.InlineKeyboardMarkup()
    mk.add(
        types.InlineKeyboardButton("📌 Назначить в класс", callback_data=f"assign_ctf:{tid}"),
        types.InlineKeyboardButton("Позже", callback_data="assign_later")
    )

    # учителю: уникальное решение + ожидаемый ответ
    bot.send_message(chat_id, f"✅ Web CTF создан: {bundle['title']}\nID: {tid}\n\n{bundle['teacher_guide']}\n\n✅ Ожидаемый ответ: {expected}", reply_markup=types.ReplyKeyboardRemove())

    bot.send_message(chat_id, "📌 Вариант задания для ученика:")
    send_code_block(chat_id, bundle["code"], reply_markup=mk)

# --------------- ASSIGNMENT CALLBACKS ---------------


def classes_kb(teacher_id: str, prefix: str):
    data=load_data()
    mk=types.InlineKeyboardMarkup()
    cls=[c for c in data["classes"].values() if isinstance(c,dict) and c.get("teacher_id")==teacher_id]
    for c in cls[:30]:
        mk.add(types.InlineKeyboardButton(c.get("name","Класс"), callback_data=f"{prefix}:{c['id']}"))
    mk.add(types.InlineKeyboardButton("Отмена", callback_data="assign_later"))
    return mk

@bot.callback_query_handler(func=lambda c: c.data.startswith("assign_test:"))
def cb_assign_test(c):
    uid=str(c.from_user.id); tid=c.data.split(":",1)[1]
    if load_data()["users"].get(uid,{}).get("role")!="teacher":
        bot.answer_callback_query(c.id,"Только учителю", show_alert=True); return
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id,"Выберите класс:", reply_markup=classes_kb(uid, f"pick_class_test:{tid}"))

@bot.callback_query_handler(func=lambda c: c.data.startswith("assign_ctf:"))
def cb_assign_ctf(c):
    uid=str(c.from_user.id); tid=c.data.split(":",1)[1]
    if load_data()["users"].get(uid,{}).get("role")!="teacher":
        bot.answer_callback_query(c.id,"Только учителю", show_alert=True); return
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id,"Выберите класс:", reply_markup=classes_kb(uid, f"pick_class_ctf:{tid}"))

@bot.callback_query_handler(func=lambda c: c.data.startswith("assign_hw:"))
def cb_assign_hw(c):
    uid=str(c.from_user.id); hid=c.data.split(":",1)[1]
    if load_data()["users"].get(uid,{}).get("role")!="teacher":
        bot.answer_callback_query(c.id,"Только учителю", show_alert=True); return
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id,"Выберите класс:", reply_markup=classes_kb(uid, f"pick_class_hw:{hid}"))

@bot.callback_query_handler(func=lambda c: c.data.startswith("pick_class_test:"))
def cb_pick_class_test(c):
    uid=str(c.from_user.id)
    rest=c.data.split("pick_class_test:",1)[1]
    tid, cid = rest.split(":",1)
    data=load_data()
    t=data["tests"].get(tid)
    if not t: bot.answer_callback_query(c.id,"Тест не найден", show_alert=True); return
    aid=gen_id("A")
    data["assignments"][aid]={"id":aid,"class_id":cid,"teacher_id":uid,"kind":"test","ref_id":tid,"title":f"Тест: {t.get('topic','')}", "created_at": now_iso()}
    data["tests"][tid]["class_id"]=cid
    save_data(data)
    bot.answer_callback_query(c.id,"Назначено ✅")
    bot.send_message(c.message.chat.id,"✅ Назначено.", reply_markup=kb_teacher())

@bot.callback_query_handler(func=lambda c: c.data.startswith("pick_class_ctf:"))
def cb_pick_class_ctf(c):
    uid=str(c.from_user.id)
    rest=c.data.split("pick_class_ctf:",1)[1]
    tid, cid = rest.split(":",1)
    data=load_data()
    t=data["ctf_tasks"].get(tid)
    if not t: bot.answer_callback_query(c.id,"CTF не найден", show_alert=True); return
    aid=gen_id("A")
    data["assignments"][aid]={"id":aid,"class_id":cid,"teacher_id":uid,"kind":"ctf","ref_id":tid,"title":f"CTF: {t.get('title','')}", "created_at": now_iso()}
    save_data(data)
    bot.answer_callback_query(c.id,"Назначено ✅")
    bot.send_message(c.message.chat.id,"✅ Назначено.", reply_markup=kb_teacher())

@bot.callback_query_handler(func=lambda c: c.data.startswith("pick_class_hw:"))
def cb_pick_class_hw(c):
    uid=str(c.from_user.id)
    rest=c.data.split("pick_class_hw:",1)[1]
    hid, cid = rest.split(":",1)
    data=load_data()
    hw=data["homeworks"].get(hid)
    if not hw: bot.answer_callback_query(c.id,"ДЗ не найдено", show_alert=True); return
    aid=gen_id("A")
    data["assignments"][aid]={
        "id":aid,
        "class_id":cid,
        "teacher_id":uid,
        "kind":"homework",
        "ref_id":hid,
        "title":f"ДЗ: {hw.get('title','')}",
        "open_at": hw.get("open_at"),
        "due_at": hw.get("due_at"),
        "remind_hours":[24,1],
        "remind_sent": {},
        "created_at": now_iso()
    }
    save_data(data)
    bot.answer_callback_query(c.id,"Назначено ✅")
    bot.send_message(c.message.chat.id,"✅ Назначено.", reply_markup=kb_teacher())

@bot.callback_query_handler(func=lambda c: c.data=="assign_later")
def cb_assign_later(c):
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id,"Ок, можно назначить позже.", reply_markup=kb_teacher())

# --------------- STUDENT: ASSIGNMENTS ---------------

@bot.message_handler(func=lambda m: m.text=="📚 Мои задания")
def s_tasks(m):
    data=load_data(); uid=str(m.from_user.id)
    u=data["users"].get(uid,{})
    if u.get("role")!="student": bot.reply_to(m,"Только ученику."); return
    cid=u.get("class_id")
    if not cid: bot.reply_to(m,"Нет класса. /start"); return
    arr=[a for a in data["assignments"].values() if isinstance(a,dict) and a.get("class_id")==cid]
    if not arr: bot.send_message(m.chat.id,"Заданий нет.", reply_markup=kb_student()); return
    arr.sort(key=lambda a:a.get("created_at",""), reverse=True)
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for a in arr[:25]:
        kb.add(f"Задание ID: {a['id']} - {a.get('title','')}")
    kb.add("❌ Отмена")
    user_states[uid]={"flow":"open_task"}
    bot.send_message(m.chat.id,"Выберите задание:", reply_markup=kb)

@bot.message_handler(func=lambda m: user_states.get(str(m.from_user.id),{}).get("flow")=="open_task")
def s_open_task(m):
    uid=str(m.from_user.id)
    if m.text=="❌ Отмена":
        user_states.pop(uid,None); bot.send_message(m.chat.id,"Ок.", reply_markup=kb_student()); return
    if "Задание ID:" not in (m.text or ""):
        bot.reply_to(m,"Выберите кнопкой."); return
    aid = m.text.split("Задание ID:",1)[1].strip().split(" - ",1)[0].strip()
    data=load_data(); a=data["assignments"].get(aid)
    if not a: bot.reply_to(m,"Не найдено."); return
    if a.get("kind")=="test":
        tid=a.get("ref_id"); test=data["tests"].get(tid)
        if not test: bot.reply_to(m,"Тест не найден."); user_states.pop(uid,None); return
        user_states[uid]={"flow":"take_test","aid":aid,"tid":tid,"i":0,"ans":[],"wrong":[]}
        q=test["questions"][0]
        opts="\n".join([f"{j+1}. {o}" for j,o in enumerate(q["options"])])
        bot.send_message(m.chat.id,f"📝 {test.get('topic')}\n\nВопрос 1:\n{q['question']}\n\n{opts}\n\nОтвет: 1-4", reply_markup=types.ReplyKeyboardRemove()); return
    if a.get("kind")=="ctf":
        tid=a.get("ref_id"); task=data["ctf_tasks"].get(tid)
        if not task: bot.reply_to(m,"CTF не найден."); user_states.pop(uid,None); return
        user_states[uid]={"flow":"solve_ctf","aid":aid,"ctf_id":tid,"attempts":0}
        chall=task.get("challenge","")
        bot.send_message(
            m.chat.id,
            f"🏁 {task.get('title')}\n\n{task.get('description','')}\n{task.get('instruction','')}\n\nОтвет одним сообщением.",
            reply_markup=types.ReplyKeyboardRemove()
        )
        send_code_block(m.chat.id, chall)
        return
    bot.reply_to(m,"Неизвестный тип."); user_states.pop(uid,None)

# --------------- STUDENT: HOMEWORK ---------------

def save_homework_res(data: Dict[str,Any], aid: str, sid: str, hw_id: str, answer: str, ok: bool):
    rid=gen_id("R")
    data["results"][rid]={
        "id":rid,
        "kind":"homework",
        "assignment_id":aid,
        "student_id":sid,
        "student_name":data["users"].get(sid,{}).get("username","student"),
        "teacher_id":data["homeworks"].get(hw_id,{}).get("teacher_id"),
        "homework_id":hw_id,
        "answer":answer,
        "format_ok":ok,
        "submitted_at":now_iso()
    }
    save_data(data)

@bot.message_handler(func=lambda m: user_states.get(str(m.from_user.id),{}).get("flow")=="submit_homework")
def s_submit_homework(m):
    uid=str(m.from_user.id); st=user_states[uid]; data=load_data()
    if m.text=="❌ Отмена":
        user_states.pop(uid,None); bot.send_message(m.chat.id,"Ок.", reply_markup=kb_student()); return
    aid=st.get("aid"); hw_id=st.get("hw_id")
    a=data["assignments"].get(aid)
    if not a:
        user_states.pop(uid,None); bot.send_message(m.chat.id,"Задание не найдено.", reply_markup=kb_student()); return
    ok, msg = assignment_can_submit(a, now_msk())
    if not ok:
        user_states.pop(uid,None); bot.send_message(m.chat.id,msg, reply_markup=kb_student()); return
    hw=data["homeworks"].get(hw_id)
    if not hw:
        user_states.pop(uid,None); bot.send_message(m.chat.id,"ДЗ не найдено.", reply_markup=kb_student()); return
    ans=(m.text or "").strip()
    fmt=hw.get("format_regex")
    if not format_ok(ans, fmt):
        bot.reply_to(m,"Неверный формат ответа. Попробуйте снова или отмените.")
        return
    save_homework_res(data, aid, uid, hw_id, ans, True)
    user_states.pop(uid,None)
    bot.send_message(m.chat.id,"✅ Принято.", reply_markup=kb_student()); return

# --------------- STUDENT: TAKE TEST ---------------

def save_test_res(data: Dict[str,Any], aid: str, sid: str, tid: str, correct: int, total: int, wrong: list):
    rid=gen_id("R")
    data["results"][rid]={"id":rid,"kind":"test","assignment_id":aid,"student_id":sid,"student_name":data["users"].get(sid,{}).get("username","student"),
                          "teacher_id":data["tests"].get(tid,{}).get("teacher_id"),"test_id":tid,
                          "correct_answers":correct,"total_questions":total,"wrong_answers":wrong,"submitted_at":now_iso()}
    save_data(data)

@bot.message_handler(func=lambda m: user_states.get(str(m.from_user.id),{}).get("flow")=="take_test")
def s_take_test(m):
    uid=str(m.from_user.id); st=user_states[uid]; data=load_data()
    tid=st["tid"]; test=data["tests"].get(tid)
    if not test: bot.send_message(m.chat.id,"Тест не найден.", reply_markup=kb_student()); user_states.pop(uid,None); return
    qs=test.get("questions",[]); i=st["i"]
    txt=(m.text or "").strip()
    if not txt.isdigit(): bot.reply_to(m,"Ответ 1-4."); return
    ans=int(txt)-1
    if ans<0 or ans>3: bot.reply_to(m,"Ответ 1-4."); return
    q=qs[i]; st["ans"].append(ans)
    if ans!=q["correct"]:
        st["wrong"].append({"question":q["question"],"user_answer":q["options"][ans],"correct_answer":q["options"][q["correct"]],"explanation":q.get("explanation","")})
    i+=1; st["i"]=i
    if i>=len(qs):
        correct=sum(1 for k,qq in enumerate(qs) if qq["correct"]==st["ans"][k])
        total=len(qs)
        save_test_res(data, st["aid"], uid, tid, correct, total, st["wrong"])
        out=[f"✅ Готово: {correct}/{total}"]
        for e in st["wrong"][:10]:
            out.append(f"\n{e['question']}\nВаш: {e['user_answer']}\nПравильный: {e['correct_answer']}")
            if e.get("explanation"): out.append(f"Пояснение: {e['explanation']}")
        user_states.pop(uid,None)
        bot.send_message(m.chat.id,"\n".join(out), reply_markup=kb_student()); return
    q=qs[i]; opts="\n".join([f"{j+1}. {o}" for j,o in enumerate(q["options"])])
    bot.send_message(m.chat.id,f"Вопрос {i+1}:\n{q['question']}\n\n{opts}\n\nОтвет: 1-4")

# --------------- STUDENT: SOLVE CTF ---------------

def save_ctf_res(data: Dict[str,Any], aid: str, sid: str, ctf_id: str, ok: bool, attempts: int):
    rid=gen_id("R")
    data["results"][rid]={"id":rid,"kind":"ctf","assignment_id":aid,"student_id":sid,"student_name":data["users"].get(sid,{}).get("username","student"),
                          "teacher_id":data["ctf_tasks"].get(ctf_id,{}).get("teacher_id"),"task_id":ctf_id,
                          "is_correct":ok,"attempts":attempts,"submitted_at":now_iso()}
    save_data(data)

@bot.message_handler(func=lambda m: user_states.get(str(m.from_user.id),{}).get("flow")=="solve_ctf")
def s_solve_ctf(m):
    uid=str(m.from_user.id); st=user_states[uid]; data=load_data()
    ctf_id=st["ctf_id"]; task=data["ctf_tasks"].get(ctf_id)
    if not task: bot.send_message(m.chat.id,"CTF не найден.", reply_markup=kb_student()); user_states.pop(uid,None); return
    st["attempts"]+=1
    ok = sha(norm(m.text)) == task.get("expected_hash")
    max_attempts = int(task.get("meta",{}).get("max_attempts",5)) if isinstance(task.get("meta"),dict) else 5
    if ok:
        save_ctf_res(data, st["aid"], uid, ctf_id, True, st["attempts"])
        user_states.pop(uid,None); bot.send_message(m.chat.id,"✅ Верно!", reply_markup=kb_student()); return
    if st["attempts"]>=max_attempts:
        save_ctf_res(data, st["aid"], uid, ctf_id, False, st["attempts"])
        user_states.pop(uid,None); bot.send_message(m.chat.id,f"❌ Неверно. Попытки закончились ({max_attempts}).", reply_markup=kb_student()); return
    bot.reply_to(m, f"❌ Неверно. Осталось попыток: {max_attempts-st['attempts']}")

# --------------- RESULTS ---------------

@bot.message_handler(func=lambda m: m.text=="📈 Мои результаты")
def s_results(m):
    data=load_data(); uid=str(m.from_user.id)
    if data["users"].get(uid,{}).get("role")!="student": bot.reply_to(m,"Только ученику."); return
    res=[r for r in data["results"].values() if isinstance(r,dict) and r.get("student_id")==uid]
    if not res: bot.send_message(m.chat.id,"Результатов нет.", reply_markup=kb_student()); return
    res.sort(key=lambda r:r.get("submitted_at",""), reverse=True)
    out=["📈 Результаты:"]
    for r in res[:30]:
        if r.get("kind")=="test":
            out.append(f"• Тест {r.get('test_id')}: {r.get('correct_answers')}/{r.get('total_questions')}")
        else:
            out.append(f"• CTF {r.get('task_id')}: {'✅' if r.get('is_correct') else '❌'} (попыток {r.get('attempts')})")
    bot.send_message(m.chat.id,"\n".join(out), reply_markup=kb_student())

@bot.message_handler(func=lambda m: m.text=="📊 Результаты")
def t_results(m):
    data=load_data(); uid=str(m.from_user.id)
    if data["users"].get(uid,{}).get("role")!="teacher": bot.reply_to(m,"Только учителю."); return
    cls=[c for c in data["classes"].values() if isinstance(c,dict) and c.get("teacher_id")==uid]
    if not cls: bot.send_message(m.chat.id,"Классов нет.", reply_markup=kb_teacher()); return
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for c in cls: kb.add(f"Класс: {c['name']}")
    kb.add("❌ Отмена")
    user_states[uid]={"flow":"tres","step":"class"}
    bot.send_message(m.chat.id,"Выберите класс:", reply_markup=kb)

@bot.message_handler(func=lambda m: user_states.get(str(m.from_user.id),{}).get("flow")=="tres")
def t_results_flow(m):
    uid=str(m.from_user.id); st=user_states[uid]; data=load_data()
    if m.text=="❌ Отмена":
        user_states.pop(uid,None); bot.send_message(m.chat.id,"Ок.", reply_markup=kb_teacher()); return
    t=(m.text or "").strip()
    if st["step"]=="class":
        if not t.startswith("Класс: "): bot.reply_to(m,"Кнопкой."); return
        name=t.replace("Класс: ","",1).strip()
        cid=None
        for k,v in data["classes"].items():
            if isinstance(v,dict) and v.get("teacher_id")==uid and v.get("name")==name: cid=k; break
        if not cid: bot.reply_to(m,"Класс не найден."); return
        st["cid"]=cid; st["step"]="student"
        studs=[(sid,u) for sid,u in data["users"].items() if isinstance(u,dict) and u.get("role")=="student" and u.get("class_id")==cid]
        if not studs: user_states.pop(uid,None); bot.send_message(m.chat.id,"Учеников нет.", reply_markup=kb_teacher()); return
        kb=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        for sid,u in studs:
            p=u.get("profile") or {}
            nm=(" ".join([p.get("last_name",""),p.get("first_name","")]).strip() or u.get("username","Ученик"))
            kb.add(f"Ученик: {nm} ({sid})")
        kb.add("❌ Отмена")
        bot.send_message(m.chat.id,"Выберите ученика:", reply_markup=kb); return
    if st["step"]=="student":
        m2=re.search(r"\((\d+)\)\s*$", t)
        if not m2: bot.reply_to(m,"Выберите кнопкой."); return
        sid=m2.group(1); cid=st["cid"]
        allowed={a["id"] for a in data["assignments"].values() if isinstance(a,dict) and a.get("class_id")==cid}
        res=[r for r in data["results"].values() if isinstance(r,dict) and r.get("student_id")==sid and r.get("assignment_id") in allowed]
        res.sort(key=lambda r:r.get("submitted_at",""), reverse=True)
        u=data["users"].get(sid,{}); p=u.get("profile") or {}
        nm=(" ".join([p.get("last_name",""),p.get("first_name","")]).strip() or u.get("username","Ученик"))
        out=[f"📊 {nm}:"]
        if not res: out.append("Нет результатов.")
        for r in res[:40]:
            if r.get("kind")=="test": out.append(f"• Тест {r.get('test_id')}: {r.get('correct_answers')}/{r.get('total_questions')}")
            else: out.append(f"• CTF {r.get('task_id')}: {'✅' if r.get('is_correct') else '❌'} (попыток {r.get('attempts')})")
        user_states.pop(uid,None)
        bot.send_message(m.chat.id,"\n".join(out), reply_markup=kb_teacher()); return

# --------------- TEACHER: TESTS VIEW + ADD/EDIT/DELETE QUESTION ---------------

def qnums_kb(n: int, title_prefix: str = "Вопрос") -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, row_width=5)
    if n <= 0:
        kb.add("❌ Назад")
        return kb
    # показываем до 30 кнопок в одном экране
    nums = [str(i) for i in range(1, min(n, 30) + 1)]
    for i in range(0, len(nums), 5):
        kb.row(*nums[i:i+5])
    kb.add("❌ Назад")
    return kb

def render_question(q: Dict[str, Any], idx: int) -> str:
    lines = [f"{idx+1}) {q.get('question','')}".strip()]
    opts = q.get("options", []) or []
    corr = q.get("correct", -1)
    for j, opt in enumerate(opts, start=1):
        mark = "✅" if (j-1) == corr else "  "
        lines.append(f"   {mark} {j}. {opt}")
    expl = q.get("explanation", "")
    if expl:
        lines.append(f"   Пояснение: {expl}")
    return "\n".join(lines)

@bot.message_handler(func=lambda m: m.text=="📚 Ваши тесты")
def t_tests(m):
    data=load_data(); uid=str(m.from_user.id)
    if data["users"].get(uid,{}).get("role")!="teacher":
        bot.reply_to(m,"Только учителю.")
        return
    tests=[t for t in data["tests"].values() if isinstance(t,dict) and t.get("teacher_id")==uid]
    if not tests:
        bot.send_message(m.chat.id,"Тестов нет.", reply_markup=kb_teacher())
        return
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for t in tests[:25]:
        kb.add(f"Тест ID: {t['id']} - {t.get('topic','')}")
    kb.add("❌ Отмена")
    user_states[uid]={"flow":"t_test_manage","step":"pick"}
    bot.send_message(m.chat.id,"Выберите тест:", reply_markup=kb)

@bot.message_handler(func=lambda m: user_states.get(str(m.from_user.id),{}).get("flow")=="t_test_manage")
def t_test_manage(m):
    uid=str(m.from_user.id); st=user_states[uid]
    data=load_data()

    def back_to_action(tid: str):
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, row_width=2)
        kb.add("👀 Просмотр","➕ Добавить вопрос")
        kb.add("✏️ Редактировать вопрос","🗑️ Удалить вопрос")
        kb.add("❌ Назад")
        st["step"]="action"
        bot.send_message(m.chat.id,"Действие?", reply_markup=kb)

    def cancel_all(msg: str = "Ок."):
        user_states.pop(uid,None)
        bot.send_message(m.chat.id,msg, reply_markup=kb_teacher())

    if m.text=="❌ Отмена":
        cancel_all("Ок.")
        return

    t=(m.text or "").strip()

    # 1) выбор теста
    if st.get("step")=="pick":
        if "Тест ID:" not in t:
            bot.reply_to(m,"Кнопкой.")
            return
        tid=t.split("Тест ID:",1)[1].strip().split(" - ",1)[0].strip()
        test=data["tests"].get(tid)
        if not test:
            bot.reply_to(m,"Не найден.")
            return
        if test.get("teacher_id") != uid:
            bot.reply_to(m,"Это не ваш тест.")
            return
        st["tid"]=tid
        back_to_action(tid)
        return

    tid=st.get("tid")
    test=data["tests"].get(tid or "", {})
    qs=test.get("questions", []) if isinstance(test, dict) else []

    # 2) меню действий
    if st.get("step")=="action":
        if t=="❌ Назад":
            cancel_all("Меню.")
            return

        if t=="👀 Просмотр":
            if not qs:
                cancel_all("В тесте пока нет вопросов.")
                return
            head=f"📚 {test.get('topic')} (ID {tid})\nВопросов: {len(qs)}"
            buf=[head]
            for i,q in enumerate(qs):
                buf.append("\n"+render_question(q, i))
            msg="\n".join(buf)
            for part in [msg[i:i+3800] for i in range(0,len(msg),3800)]:
                bot.send_message(m.chat.id, part)
            cancel_all("Готово.")
            return

        if t=="➕ Добавить вопрос":
            st["step"]="q_text"
            st["new"]={"options":[]}
            bot.send_message(m.chat.id,"Текст вопроса?", reply_markup=kb_cancel())
            return

        if t=="✏️ Редактировать вопрос":
            if not qs:
                bot.reply_to(m,"В тесте нет вопросов для редактирования.")
                return
            st["step"]="pick_q_edit"
            bot.send_message(m.chat.id, "Номер вопроса для редактирования:", reply_markup=qnums_kb(len(qs)))
            return

        if t=="🗑️ Удалить вопрос":
            if not qs:
                bot.reply_to(m,"В тесте нет вопросов для удаления.")
                return
            st["step"]="pick_q_del"
            bot.send_message(m.chat.id, "Номер вопроса для удаления:", reply_markup=qnums_kb(len(qs)))
            return

        bot.reply_to(m,"Выберите кнопкой.")
        return

    # 3) добавление вопроса (как раньше)
    if st.get("step")=="q_text":
        if t=="❌ Отмена":
            cancel_all("Отменено.")
            return
        st["new"]["question"]=t
        st["step"]="opt1"
        bot.send_message(m.chat.id,"Вариант 1?", reply_markup=kb_cancel())
        return

    if st.get("step","").startswith("opt"):
        if t=="❌ Отмена":
            cancel_all("Отменено.")
            return
        idx=int(st["step"].replace("opt",""))
        st["new"]["options"].append(t)
        if idx<4:
            st["step"]=f"opt{idx+1}"
            bot.send_message(m.chat.id,f"Вариант {idx+1}?", reply_markup=kb_cancel())
            return
        st["step"]="correct"
        bot.send_message(m.chat.id,"Номер правильного (1-4)?", reply_markup=kb_cancel())
        return

    if st.get("step")=="correct":
        if t=="❌ Отмена":
            cancel_all("Отменено.")
            return
        if not t.isdigit() or int(t) not in (1,2,3,4):
            bot.reply_to(m,"1-4.")
            return
        st["new"]["correct"]=int(t)-1
        st["step"]="expl"
        bot.send_message(m.chat.id,"Пояснение (или '-')?", reply_markup=kb_cancel())
        return

    if st.get("step")=="expl":
        if t=="❌ Отмена":
            cancel_all("Отменено.")
            return
        st["new"]["explanation"]="" if t=="-" else t
        data=load_data()
        if tid not in data["tests"]:
            cancel_all("Тест не найден.")
            return
        data["tests"][tid].setdefault("questions", []).append(st["new"])
        data["tests"][tid]["updated_at"]=now_iso()
        save_data(data)
        cancel_all("✅ Добавлено.")
        return

    # 4) выбор вопроса для редактирования
    if st.get("step")=="pick_q_edit":
        if t=="❌ Назад":
            back_to_action(tid)
            return
        if not t.isdigit():
            bot.reply_to(m,"Введите номер вопроса кнопкой.")
            return
        qi=int(t)-1
        if qi<0 or qi>=len(qs):
            bot.reply_to(m,"Нет такого вопроса.")
            return
        st["q_index"]=qi
        st["step"]="edit_menu"
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, row_width=2)
        kb.add("👁 Показать вопрос","✏️ Изменить текст")
        kb.add("✏️ Вариант 1","✏️ Вариант 2")
        kb.add("✏️ Вариант 3","✏️ Вариант 4")
        kb.add("✅ Изменить правильный","📝 Изменить пояснение")
        kb.add("❌ Назад")
        bot.send_message(m.chat.id, "Что редактируем?", reply_markup=kb)
        return

    # 5) меню редактирования
    if st.get("step")=="edit_menu":
        if t=="❌ Назад":
            back_to_action(tid)
            return
        qi=st.get("q_index", 0)
        if qi<0 or qi>=len(qs):
            back_to_action(tid)
            return

        if t=="👁 Показать вопрос":
            bot.send_message(m.chat.id, render_question(qs[qi], qi))
            # остаёмся в edit_menu
            return

        if t=="✏️ Изменить текст":
            st["step"]="edit_q_text"
            bot.send_message(m.chat.id,"Новый текст вопроса:", reply_markup=kb_cancel())
            return

        if t in ("✏️ Вариант 1","✏️ Вариант 2","✏️ Вариант 3","✏️ Вариант 4"):
            opt_i=int(t.split()[-1])-1
            st["opt_i"]=opt_i
            st["step"]="edit_opt"
            bot.send_message(m.chat.id, f"Новый текст для варианта {opt_i+1}:", reply_markup=kb_cancel())
            return

        if t=="✅ Изменить правильный":
            st["step"]="edit_correct"
            bot.send_message(m.chat.id,"Номер правильного ответа (1-4)?", reply_markup=kb_cancel())
            return

        if t=="📝 Изменить пояснение":
            st["step"]="edit_expl"
            bot.send_message(m.chat.id,"Новое пояснение (или '-' чтобы очистить):", reply_markup=kb_cancel())
            return

        bot.reply_to(m,"Выберите кнопкой.")
        return

    # 6) применение редактирования (сохранение сразу)
    if st.get("step") in ("edit_q_text","edit_opt","edit_correct","edit_expl"):
        if t=="❌ Отмена":
            st["step"]="edit_menu"
            kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, row_width=2)
            kb.add("👁 Показать вопрос","✏️ Изменить текст")
            kb.add("✏️ Вариант 1","✏️ Вариант 2")
            kb.add("✏️ Вариант 3","✏️ Вариант 4")
            kb.add("✅ Изменить правильный","📝 Изменить пояснение")
            kb.add("❌ Назад")
            bot.send_message(m.chat.id,"Ок, не меняем. Что дальше?", reply_markup=kb)
            return

        data=load_data()
        test=data["tests"].get(tid)
        if not test or test.get("teacher_id")!=uid:
            cancel_all("Тест не найден.")
            return
        qs2=test.get("questions", [])
        qi=int(st.get("q_index", 0))
        if qi<0 or qi>=len(qs2):
            st["step"]="edit_menu"
            bot.reply_to(m,"Вопрос не найден.")
            return

        if st["step"]=="edit_q_text":
            qs2[qi]["question"]=t.strip()

        elif st["step"]=="edit_opt":
            opt_i=int(st.get("opt_i", 0))
            qs2[qi].setdefault("options", ["","","",""])
            if not isinstance(qs2[qi]["options"], list) or len(qs2[qi]["options"])!=4:
                qs2[qi]["options"] = (qs2[qi].get("options") or [])[:4]
                while len(qs2[qi]["options"])<4: qs2[qi]["options"].append("")
            qs2[qi]["options"][opt_i]=t.strip()

        elif st["step"]=="edit_correct":
            if not t.isdigit() or int(t) not in (1,2,3,4):
                bot.reply_to(m,"1-4.")
                return
            qs2[qi]["correct"]=int(t)-1

        elif st["step"]=="edit_expl":
            qs2[qi]["explanation"]="" if t.strip()=="-" else t.strip()

        test["questions"]=qs2
        test["updated_at"]=now_iso()
        save_data(data)

        st["step"]="edit_menu"
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, row_width=2)
        kb.add("👁 Показать вопрос","✏️ Изменить текст")
        kb.add("✏️ Вариант 1","✏️ Вариант 2")
        kb.add("✏️ Вариант 3","✏️ Вариант 4")
        kb.add("✅ Изменить правильный","📝 Изменить пояснение")
        kb.add("❌ Назад")
        bot.send_message(m.chat.id,"✅ Изменено. Что дальше?", reply_markup=kb)
        return

    # 7) удаление вопроса
    if st.get("step")=="pick_q_del":
        if t=="❌ Назад":
            back_to_action(tid)
            return
        if not t.isdigit():
            bot.reply_to(m,"Введите номер вопроса кнопкой.")
            return
        qi=int(t)-1
        if qi<0 or qi>=len(qs):
            bot.reply_to(m,"Нет такого вопроса.")
            return
        st["q_index"]=qi
        st["step"]="confirm_del"
        q_preview = render_question(qs[qi], qi)
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, row_width=2)
        kb.add("🗑️ Да, удалить","❌ Нет")
        bot.send_message(m.chat.id, f"Удалить этот вопрос?\n\n{q_preview}", reply_markup=kb)
        return

    if st.get("step")=="confirm_del":
        if t=="❌ Нет":
            back_to_action(tid)
            return
        if t!="🗑️ Да, удалить":
            bot.reply_to(m,"Выберите кнопкой.")
            return
        data=load_data()
        test=data["tests"].get(tid)
        if not test or test.get("teacher_id")!=uid:
            cancel_all("Тест не найден.")
            return
        qs2=test.get("questions", [])
        qi=int(st.get("q_index", 0))
        if 0 <= qi < len(qs2):
            qs2.pop(qi)
            test["questions"]=qs2
            test["updated_at"]=now_iso()
            save_data(data)
            bot.send_message(m.chat.id,"🗑️ Удалено.")
            back_to_action(tid)
            return
        back_to_action(tid)
        bot.send_message(m.chat.id,"Вопрос уже отсутствует.")
        return

    # fallback внутри manage
    bot.reply_to(m,"Используйте кнопки меню.")

# --------------- TEACHER: CTF VIEW + ASSIGN ---------------

@bot.message_handler(func=lambda m: m.text=="🏁 Ваши CTF")
def t_ctf_list(m):
    data=load_data(); uid=str(m.from_user.id)
    if data["users"].get(uid,{}).get("role")!="teacher":
        bot.reply_to(m,"Только учителю.")
        return
    tasks=[t for t in data["ctf_tasks"].values() if isinstance(t,dict) and t.get("teacher_id")==uid]
    if not tasks:
        bot.send_message(m.chat.id,"CTF заданий нет.", reply_markup=kb_teacher())
        return
    tasks.sort(key=lambda x:x.get("created_at",""), reverse=True)
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for t in tasks[:25]:
        title=t.get("title") or f"CTF {t.get('id','')}"
        kb.add(f"CTF ID: {t['id']} - {title}")
    kb.add("❌ Отмена")
    user_states[uid]={"flow":"t_ctf_manage","step":"pick"}
    bot.send_message(m.chat.id,"Выберите CTF:", reply_markup=kb)

@bot.message_handler(func=lambda m: user_states.get(str(m.from_user.id),{}).get("flow")=="t_ctf_manage")
def t_ctf_manage(m):
    uid=str(m.from_user.id); st=user_states[uid]; data=load_data()
    if m.text=="❌ Отмена":
        user_states.pop(uid,None)
        bot.send_message(m.chat.id,"Ок.", reply_markup=kb_teacher())
        return
    t=(m.text or "").strip()
    if st.get("step")=="pick":
        if "CTF ID:" not in t:
            bot.reply_to(m,"Выберите кнопкой.")
            return
        cid=t.split("CTF ID:",1)[1].strip().split(" - ",1)[0].strip()
        task=data["ctf_tasks"].get(cid)
        if not task:
            bot.reply_to(m,"CTF не найден.")
            return
        st["ctf_id"]=cid; st["step"]="action"
        kb=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, row_width=2)
        kb.add("👀 Просмотр","📌 Назначить в класс")
        kb.add("❌ Назад")
        bot.send_message(m.chat.id,"Действие?", reply_markup=kb)
        return

    if st.get("step")=="action":
        if t=="❌ Назад":
            user_states.pop(uid,None)
            bot.send_message(m.chat.id,"Меню.", reply_markup=kb_teacher())
            return
        ctf_id=st["ctf_id"]
        task=data["ctf_tasks"].get(ctf_id)
        if not task:
            user_states.pop(uid,None)
            bot.send_message(m.chat.id,"CTF не найден.", reply_markup=kb_teacher())
            return

        if t=="📌 Назначить в класс":
            user_states.pop(uid,None)
            bot.send_message(m.chat.id,"Выберите класс:", reply_markup=classes_kb(uid, f"pick_class_ctf:{ctf_id}"))
            return

        if t=="👀 Просмотр":
            title=task.get("title","CTF")
            kind=task.get("kind","ctf")
            subtype=task.get("subtype","-")
            desc=task.get("description","")
            instr=task.get("instruction","")
            chall=task.get("challenge","")
            expected=task.get("expected_plain")
            tguide=task.get("teacher_guide","")
            head=[f"🏁 {title} (ID {ctf_id})", f"Тип: {kind}/{subtype}"]
            if desc: head.append(f"\nОписание: {desc}")
            if instr: head.append(f"Инструкция: {instr}")
            if tguide: head.append(f"\n\n{tguide}")
            if expected:
                head.append(f"\n✅ Ожидаемый ответ (для учителя): {expected}")
            else:
                head.append("\n✅ Ожидаемый ответ: (не сохранён, проверка идёт по хешу)")

            msg="\n".join(head)
            for part in [msg[i:i+3500] for i in range(0,len(msg),3500)]:
                bot.send_message(m.chat.id, part)

            # challenge отдельно, чтобы не упираться в лимит сообщения
            if chall:
                bot.send_message(m.chat.id, "\nФайл/код задания:")
                send_code_block(m.chat.id, chall)

            user_states.pop(uid,None)
            bot.send_message(m.chat.id,"Готово.", reply_markup=kb_teacher())
            return

        bot.reply_to(m,"Выберите кнопкой.")
        return

# --------------- HELP ---------------

@bot.message_handler(func=lambda m: m.text=="ℹ️ Помощь")
def help_msg(m):
    data=load_data(); uid=str(m.from_user.id); role=data["users"].get(uid,{}).get("role")
    if role=="teacher":
        bot.send_message(m.chat.id,"Учитель: создайте класс → получите код → создайте тест/CTF → назначьте в класс. Результаты: выберите класс и ученика.", reply_markup=kb_teacher())
    elif role=="student":
        bot.send_message(m.chat.id,"Ученик: откройте «Мои задания», решайте тесты/CTF. «Мои результаты» — история.", reply_markup=kb_student())
    else:
        bot.send_message(m.chat.id,"Нажмите /start для регистрации.")

@bot.message_handler(func=lambda m: True)
def fallback(m):
    if user_states.get(str(m.from_user.id)): return
    bot.reply_to(m, "Не понял. Нажмите /start или используйте кнопки меню.")

if __name__ == "__main__":
    bot.infinity_polling(skip_pending=True)
