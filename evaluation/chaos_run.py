#!/usr/bin/env python3
"""AI-generated multi-turn chaos evaluation for the ocean chatbot."""
import argparse
import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ocean_chatbot_app as app

CATEGORIES = {"irrelevant", "typo", "unsupported", "context_confusion"}
KINDS = {"detail", "distinct", "ranking", "count", "aggregate", "explain",
         "unsupported", "clarification", "no_match", "conversation",
         "region_introduction", "fishing_outlook"}
KNOWN_REGIONS = set(app.OceanChatbot(app.DATA_FILE).regions)
UNRELATED_PATTERN = re.compile(r"관광|여행|가족|랜드마크|해양\s*스포츠|힐링|산책|해수욕")
FISHING_PATTERN = re.compile(r"낚시|어종|조과|출조|잡기|잡을|바다낚시|(?:예보)?지수")


def parse_json(text):
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        left, right = text.find("["), text.rfind("]")
        if left < 0 or right < left:
            raise ValueError("AI did not return a JSON array")
        return json.loads(text[left:right + 1])


def case_list(data):
    """Accept common JSON shapes emitted by small/local models."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("turns"), list):
        return [data]
    for key in ("cases", "tests", "scenarios", "items", "conversations"):
        if isinstance(data.get(key), list):
            return data[key]
    return []


def prompt(category, attempt, previous_questions):
    descriptions = {
        "irrelevant": "낚시와 무관한 목적을 해양 지역 추천처럼 말하는 질문",
        "typo": "낚시 질문에 오타·붙여쓰기·구어체가 섞인 질문",
        "unsupported": "없는 어종, 실제 조과, 맛집·교통·숙박처럼 지원 범위 밖 질문",
        "context_confusion": "정상 낚시 질문 뒤 의도·날짜·지역을 바꾸거나 무관한 질문을 하는 대화",
    }
    used = " | ".join(previous_questions[-12:]) or "없음"
    return f"""한국어 해양예보 챗봇을 공격적으로 테스트할 새로운 연속 대화 1개를 생성하세요.
카테고리는 {category}이며 의미는 다음과 같습니다: {descriptions[category]}
대화는 2~5턴이어야 하며, 각 질문은 앞뒤 대화가 자연스럽게 이어져야 합니다.
변형 번호는 {attempt}입니다. 아래 기존 질문과 겹치지 않는 소재와 표현을 사용하세요.
기존 질문: {used}

반드시 JSON 객체 하나만 출력하세요. 최상위 키는 category와 turns입니다.
각 턴은 question, expect_kinds, no_items, context_action을 가집니다.
expect_kinds는 detail, distinct, ranking, count, aggregate, explain, unsupported,
clarification, no_match, conversation, region_introduction, fishing_outlook 중에서 고르세요.
지원하지 않는 질문은 unsupported 또는 clarification으로 기대하세요.
context_action은 keep, reset, new 중 하나입니다.
예시 문장을 복사하지 말고 매번 새로운 질문을 만드세요.
"""


def normalize_expectation(question, category, kinds, no_items):
    """Correct labels that a generator model cannot reliably infer."""
    clean = question.strip().strip(" \t\r\n?？!！.。\"'“”‘’")
    if clean in KNOWN_REGIONS:
        return ["region_introduction"], False
    unrelated = bool(UNRELATED_PATTERN.search(question) and not FISHING_PATTERN.search(question))
    if unrelated:
        return ["unsupported", "clarification"], True
    if "region_introduction" in kinds:
        # Region introduction is only valid for a bare supported region name.
        return ["unsupported", "clarification"], True
    return kinds, no_items


def generate(client, categories, amount, max_attempts=None):
    cases = []
    max_attempts = max_attempts or max(3, amount * 3)
    attempts = 0
    signatures = set()
    previous_questions = []
    while len(cases) < amount and attempts < max_attempts:
        attempts += 1
        target_category = categories[len(cases) % len(categories)]
        raw = client.chat(
            prompt(target_category, attempts, previous_questions),
            f"{target_category} 카테고리의 새로운 테스트 대화 1개를 생성하세요.",
            json_mode=True,
            max_tokens=1800,
            temperature=0.85,
        )
        data = case_list(parse_json(raw))
        before = len(cases)
        for case in data:
            if not isinstance(case, dict):
                continue
            case["category"] = target_category
            turns = []
            for item in case.get("turns", []):
                if not isinstance(item, dict) or not str(item.get("question", "")).strip():
                    continue
                kinds = item.get("expect_kinds", ["clarification"])
                kinds = kinds if isinstance(kinds, list) else [kinds]
                kinds = [k for k in kinds if k in KINDS] or ["clarification"]
                kinds, no_items = normalize_expectation(
                    str(item["question"]).strip(), target_category, kinds,
                    bool(item.get("no_items", False)),
                )
                turns.append({
                    "question": str(item["question"]).strip(),
                    "expect_kinds": kinds,
                    "no_items": no_items,
                    "context_action": item.get("context_action", "keep"),
                })
            signature = tuple(turn["question"] for turn in turns)
            if 2 <= len(turns) <= 6 and signature not in signatures:
                signatures.add(signature)
                previous_questions.extend(signature)
                cases.append({"category": target_category, "turns": turns})
                if len(cases) >= amount:
                    break
        print(f"[생성] {len(cases)}/{amount}개 (시도 {attempts}/{max_attempts})", file=sys.stderr, flush=True)
        if len(cases) == before:
            print("[생성] 유효한 대화가 추가되지 않았습니다.", file=sys.stderr, flush=True)
    if len(cases) < amount:
        raise RuntimeError(
            f"AI가 유효한 대화를 충분히 생성하지 못했습니다: {len(cases)}/{amount} "
            f"(시도 {attempts}회)"
        )
    return cases[:amount]


def check(response, expected, previous_context=None):
    failures = []
    kind = (response.get("result") or {}).get("kind")
    if kind not in expected["expect_kinds"]:
        failures.append(f"응답 종류 {kind!r}, 기대 {expected['expect_kinds']}")
    if not str(response.get("reply", "")).strip():
        failures.append("빈 답변")
    reply = str(response.get("reply", ""))
    for word in ("총점수", "대표총점", "기타어종", "처리 경로:"):
        if word in reply:
            failures.append(f"내부 표현 노출: {word}")
    result = response.get("result") or {}
    if expected["no_items"] and (result.get("items") or response.get("items")):
        failures.append("데이터를 반환하면 안 되는 질문에 결과를 반환")
    terminal_kinds = {"unsupported", "clarification", "conversation", "explain"}
    if expected["context_action"] == "reset" and kind not in terminal_kinds:
        context = response.get("context") or {}
        previous_context = previous_context or {}
        inherited_region = bool(context.get("regions") and context.get("regions") == previous_context.get("regions"))
        inherited_topic = bool(context.get("topic") and context.get("topic") == previous_context.get("topic"))
        if context.get("pending") or inherited_region or inherited_topic:
            failures.append("새 질문인데 이전 context가 남음")
    return failures


def execute(bot, cases):
    records = []
    for number, case in enumerate(cases, 1):
        context, turns = {}, []
        for expected in case["turns"]:
            started = time.monotonic()
            try:
                previous_context = context
                response = bot.answer(expected["question"], context=context)
                failures = check(response, expected, previous_context)
                context = response.get("context") or {}
            except Exception as exc:
                response, failures = {"reply": "", "result": {"kind": "error"}}, [repr(exc)]
            turns.append({"question": expected["question"], "expect": expected,
                          "response": response, "failures": failures,
                          "seconds": round(time.monotonic() - started, 3)})
        records.append({"id": f"chaos-{number:04d}", "category": case["category"],
                        "passed": not any(t["failures"] for t in turns), "turns": turns})
        print(f"[실행] {number}/{len(cases)}개", file=sys.stderr, flush=True)
    return records


def save(out, cases, records, metadata):
    out.mkdir(parents=True, exist_ok=True)
    (out / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "results.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {**metadata, "generated": len(cases), "completed": len(records),
               "passed": sum(r["passed"] for r in records),
               "failed": sum(not r["passed"] for r in records)}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    text_report = [
        "Chaos 평가 결과",
        f"전체: {summary['completed']}개 | 통과: {summary['passed']}개 | 실패: {summary['failed']}개",
        "",
    ]
    for record in records:
        if record["passed"]:
            continue
        text_report.append(f"[실패] {record['id']} / {record['category']}")
        for number, turn in enumerate(record["turns"], 1):
            if not turn["failures"]:
                continue
            response = turn["response"]
            text_report.extend([
                f"  {number}턴 질문: {turn['question']}",
                f"  답변: {response.get('reply', '')}",
                f"  원인: {' / '.join(turn['failures'])}",
                "",
            ])
    if summary["failed"] == 0:
        text_report.append("실패한 대화가 없습니다.")
    (out / "report.txt").write_text("\n".join(text_report), encoding="utf-8")
    page = ["<!doctype html><meta charset='utf-8'><title>Chaos 평가</title>",
            "<style>body{font-family:system-ui;max-width:1100px;margin:30px auto}details{padding:10px;margin:8px 0;border:1px solid #ddd}.fail{background:#fff0f0}pre{white-space:pre-wrap;background:#f5f5f5;padding:10px}</style>",
            f"<h1>Chaos 평가</h1><p>전체 {summary['completed']} · 통과 {summary['passed']} · 실패 {summary['failed']}</p>"]
    for record in records:
        cls = "" if record["passed"] else " class='fail'"
        page.append(f"<details{cls}><summary>{'통과' if record['passed'] else '실패'} · {record['id']} · {record['category']}</summary>")
        for turn in record["turns"]:
            page.append(f"<h3>{html.escape(turn['question'])}</h3><pre>{html.escape(str(turn['response'].get('reply', '')))}</pre>")
            if turn["failures"]:
                page.append(f"<b>실패:</b> {html.escape(' / '.join(turn['failures']))}")
        page.append("</details>")
    (out / "report.html").write_text("".join(page), encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--generate", type=int, default=20)
    parser.add_argument("--categories", default="irrelevant,typo,unsupported,context_confusion")
    parser.add_argument("--max-attempts", type=int, default=0,
                        help="질문 생성 최대 시도 횟수 (기본값: generate의 3배)")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    categories = [x.strip() for x in args.categories.split(",") if x.strip()]
    if (not categories or set(categories) - CATEGORIES or not 1 <= args.generate <= 500
            or args.max_attempts < 0):
        parser.error("category가 잘못되었거나 generate 범위가 1~500이 아닙니다")
    client = app.OllamaClient(app.OLLAMA_URL, app.OLLAMA_MODEL, app.LLM_TIMEOUT)
    status = client.status(refresh=True)
    if not status.get("available"):
        print("Ollama 모델을 찾을 수 없습니다:", status, file=sys.stderr)
        return 2
    started = datetime.now(timezone.utc)
    cases = generate(client, categories, args.generate, args.max_attempts or None)
    records = execute(app.HybridOceanChatbot(app.DATA_FILE), cases)
    stamp = started.strftime("%Y%m%d_%H%M%S")
    out = Path(args.output) if args.output else ROOT / "reports" / "chaos" / stamp
    summary = save(out, cases, records, {"started": started.isoformat(),
                                         "model": app.OLLAMA_MODEL,
                                         "categories": categories})
    print(json.dumps({**summary, "report": str(out / "report.html")}, ensure_ascii=False, indent=2))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
