"""ctf_yagpt_unique.py

Drop-in module: generate UNIQUE CTF tasks via YandexGPT.
- Crypto CTF: model generates unique scenario text + unique teacher guide.
  You (bot) then encrypt/obfuscate locally and store task.
- Web CTF: model generates unique vulnerable code snippet (educational code-review format)
  + unique teacher guide (NO exploit payloads).

Uniqueness: store fingerprints in your bot_data.json under data['ctf_fingerprints'] (list of hashes).
If generated content repeats, module retries with a new nonce.

NOTE: This module does NOT depend on your bot framework; it only provides async generators.
"""

from __future__ import annotations

import os
import re
import json
import hashlib
import secrets
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

YANDEX_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"


def sha256_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8", errors="ignore")).hexdigest()


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _extract_block(text: str, tag: str) -> str:
    """Extracts a block like:
    <TAG>
    ...
    </TAG>
    Returns '' if not found.
    """
    if not text:
        return ""
    m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, flags=re.S | re.I)
    return (m.group(1).strip() if m else "")


async def yagpt_completion(
    *,
    api_key: str,
    folder_id: str,
    messages: List[Dict[str, str]],
    temperature: float = 0.8,
    max_tokens: int = 1800,
    model: str = "yandexgpt-lite",
    timeout_s: int = 60,
) -> str:
    headers = {
        "Authorization": f"Api-Key {api_key}",
        "x-folder-id": folder_id,
        "Content-Type": "application/json",
    }
    payload = {
        "modelUri": f"gpt://{folder_id}/{model}",
        "completionOptions": {
            "stream": False,
            "temperature": temperature,
            "maxTokens": max_tokens,
        },
        "messages": messages,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(YANDEX_URL, headers=headers, json=payload, timeout=timeout_s) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"YandexGPT HTTP {resp.status}: {body[:300]}")
            data = await resp.json()
            alts = data.get("result", {}).get("alternatives", [])
            if not alts:
                return ""
            return alts[0].get("message", {}).get("text", "") or ""


def ensure_fingerprint_store(data: Dict[str, Any]) -> List[str]:
    """Ensure data has a list for fingerprints."""
    if "ctf_fingerprints" not in data or not isinstance(data.get("ctf_fingerprints"), list):
        data["ctf_fingerprints"] = []
    return data["ctf_fingerprints"]


def is_duplicate(data: Dict[str, Any], fingerprint: str) -> bool:
    fps = ensure_fingerprint_store(data)
    return fingerprint in set(fps)


def remember_fingerprint(data: Dict[str, Any], fingerprint: str, keep_last: int = 2000) -> None:
    fps = ensure_fingerprint_store(data)
    fps.append(fingerprint)
    if len(fps) > keep_last:
        # keep only last N to limit file size
        del fps[:-keep_last]


async def generate_crypto_text_and_guides(
    *,
    api_key: str,
    folder_id: str,
    topic: str,
    flag: str,
    subtype: str,
    params: Dict[str, Any],
    attempt_nonce: str,
) -> Dict[str, str]:
    """Ask YandexGPT for unique plaintext (must include provided flag exactly once),
    plus student hint + teacher guide. Output is parsed from tagged blocks.

    Returns: {"plaintext":..., "student_hint":..., "teacher_guide":..., "title":...}
    """

    # We keep it safe: educational instructions only.
    sys = (
        "Ты — преподаватель по кибербезопасности и автор CTF. "
        "Создавай УНИКАЛЬНЫЕ задания. Никогда не повторяй тексты и объяснения дословно. "
        "Пиши по-русски."
    )

    # subtype-specific explanation should reference params.
    user = f"""
Сгенерируй уникальную заготовку для Crypto CTF.

Требования:
- Тема/контекст текста: {topic}
- Вставь флаг ТОЧНО в таком виде, РОВНО ОДИН РАЗ: {flag}
- Длина текста: 3–7 предложений, без списков.
- Текст должен быть правдоподобным и отличаться от типовых шаблонов.

Тип шифрования: {subtype}
Параметры (используй их в объяснении): {json.dumps(params, ensure_ascii=False)}

Сгенерируй ответ СТРОГО в таком формате (с тегами):
<TITLE>...</TITLE>
<PLAINTEXT>...</PLAINTEXT>
<STUDENT_HINT>Короткая подсказка ученику (1-2 предложения), не раскрывай флаг.</STUDENT_HINT>
<TEACHER_GUIDE>Пошаговая инструкция учителю (4-8 шагов) как расшифровать/деобфусцировать и найти флаг. Упомяни параметры.</TEACHER_GUIDE>

Уникальность (nonce): {attempt_nonce}
""".strip()

    text = await yagpt_completion(
        api_key=api_key,
        folder_id=folder_id,
        messages=[{"role": "system", "text": sys}, {"role": "user", "text": user}],
        temperature=0.9,
        max_tokens=1400,
    )

    title = _extract_block(text, "TITLE") or "Crypto CTF"
    plaintext = _extract_block(text, "PLAINTEXT")
    student_hint = _extract_block(text, "STUDENT_HINT")
    teacher_guide = _extract_block(text, "TEACHER_GUIDE")

    # minimal validation
    if flag not in plaintext:
        # fail fast so caller can retry
        raise ValueError("Model output did not include the required flag in PLAINTEXT")

    # ensure exactly one occurrence
    if plaintext.count(flag) != 1:
        raise ValueError("PLAINTEXT must contain the flag exactly once")

    return {
        "title": title.strip(),
        "plaintext": plaintext.strip(),
        "student_hint": student_hint.strip() or "Попробуйте восстановить текст и найти флаг lapin{...}.",
        "teacher_guide": teacher_guide.strip() or "1) Расшифруйте. 2) Найдите lapin{...}.",
    }


async def generate_web_code_and_guides(
    *,
    api_key: str,
    folder_id: str,
    vuln_type: str,
    embedded_flag: str,
    expected_answer: str,
    attempt_nonce: str,
) -> Dict[str, str]:
    """Ask YandexGPT for unique vulnerable code snippet (educational code-review) + teacher guide.

    Returns: {"title","description","code","student_instruction","teacher_guide"}

    SAFETY: prompt explicitly forbids exploit payloads; solution is via code review.
    """

    sys = (
        "Ты — преподаватель по безопасной веб-разработке. "
        "Создавай УНИКАЛЬНЫЕ учебные задания формата code review. "
        "НЕ давай эксплуатационные payload'ы, НЕ описывай взлом реальных систем. "
        "Только анализ кода и объяснение, что не так и где находится ответ."
    )

    user = f"""
Сгенерируй уникальное Web CTF задание (формат: студент читает код и находит ответ).

Тема уязвимости: {vuln_type}
Встроенный флаг в код (используй как константу/переменную): {embedded_flag}
Ожидаемый ответ, который студент должен отправить боту (может совпадать с флагом или отличаться): {expected_answer}

Требования к коду:
- Один файл, минимальный пример (предпочтительно Python Flask или Node/Express).
- Код должен содержать уязвимость по теме и комментарий/кусок логики, где спрятан встроенный флаг.
- НЕ добавляй инструкции по эксплуатации (никаких payload'ов). Решение должно быть возможно через чтение кода.

Сгенерируй ответ СТРОГО в таком формате (с тегами):
<TITLE>...</TITLE>
<DESCRIPTION>1-2 абзаца задания для ученика</DESCRIPTION>
<STUDENT_INSTRUCTION>1 короткий абзац: что сделать (прочитать код, найти проблему, отправить ответ)</STUDENT_INSTRUCTION>
<CODE>...только код, без пояснений...</CODE>
<TEACHER_GUIDE>Пошагово 5-9 шагов: что посмотреть в коде, как понять уязвимость и где увидеть ответ/флаг. Без payload'ов.</TEACHER_GUIDE>

Уникальность (nonce): {attempt_nonce}
""".strip()

    text = await yagpt_completion(
        api_key=api_key,
        folder_id=folder_id,
        messages=[{"role": "system", "text": sys}, {"role": "user", "text": user}],
        temperature=0.9,
        max_tokens=2200,
    )

    title = _extract_block(text, "TITLE") or f"Web CTF: {vuln_type}"
    desc = _extract_block(text, "DESCRIPTION")
    instr = _extract_block(text, "STUDENT_INSTRUCTION")
    code = _extract_block(text, "CODE")
    guide = _extract_block(text, "TEACHER_GUIDE")

    if not code or len(code) < 80:
        raise ValueError("Model output did not produce a CODE block")

    # ensure embedded_flag appears, otherwise teacher cannot verify quickly
    if embedded_flag not in code:
        raise ValueError("CODE must contain the embedded_flag")

    return {
        "title": title.strip(),
        "description": (desc.strip() or "Проанализируйте код и найдите ответ."),
        "student_instruction": (instr.strip() or "Прочитайте код, найдите проблему и отправьте ответ одним сообщением."),
        "code": code.strip(),
        "teacher_guide": (guide.strip() or "1) Прочитайте код. 2) Найдите уязвимость и место, где спрятан ответ."),
    }


async def generate_unique_crypto_bundle(
    *,
    data: Dict[str, Any],
    api_key: str,
    folder_id: str,
    topic: str,
    flag: str,
    subtype: str,
    params: Dict[str, Any],
    max_attempts: int = 4,
) -> Dict[str, str]:
    """Returns unique (within data['ctf_fingerprints']) text+guides for crypto."""

    for attempt in range(1, max_attempts + 1):
        nonce = f"{secrets.token_hex(6)}-{attempt}"
        out = await generate_crypto_text_and_guides(
            api_key=api_key,
            folder_id=folder_id,
            topic=topic,
            flag=flag,
            subtype=subtype,
            params=params,
            attempt_nonce=nonce,
        )
        fp = sha256_text(out["plaintext"] + "\n" + out["teacher_guide"])
        if not is_duplicate(data, fp):
            remember_fingerprint(data, fp)
            return out

    # if all attempts duplicate, still return last one but mark it
    out["teacher_guide"] = out.get("teacher_guide", "") + "\n\n⚠️ Не удалось гарантировать уникальность после нескольких попыток."
    return out


async def generate_unique_web_bundle(
    *,
    data: Dict[str, Any],
    api_key: str,
    folder_id: str,
    vuln_type: str,
    embedded_flag: str,
    expected_answer: str,
    max_attempts: int = 4,
) -> Dict[str, str]:
    """Returns unique (within data['ctf_fingerprints']) web code+guides."""

    for attempt in range(1, max_attempts + 1):
        nonce = f"{secrets.token_hex(6)}-{attempt}"
        out = await generate_web_code_and_guides(
            api_key=api_key,
            folder_id=folder_id,
            vuln_type=vuln_type,
            embedded_flag=embedded_flag,
            expected_answer=expected_answer,
            attempt_nonce=nonce,
        )
        fp = sha256_text(out["code"] + "\n" + out["teacher_guide"])
        if not is_duplicate(data, fp):
            remember_fingerprint(data, fp)
            return out

    out["teacher_guide"] = out.get("teacher_guide", "") + "\n\n⚠️ Не удалось гарантировать уникальность после нескольких попыток."
    return out
