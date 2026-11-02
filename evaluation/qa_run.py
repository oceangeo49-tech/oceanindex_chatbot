#!/usr/bin/env python3
"""Generate conversations, run the chatbot, and produce a human-readable AI QA report."""
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
from evaluation.chaos_run import CATEGORIES, generate, parse_json


STATUS_LABELS = {"pass": "정상", "fail": "문제 있음", "review": "확인 필요"}
STATUS_ICONS = {"pass": "OK", "fail": "X", "review": "?"}
KNOWN_FISH = set(app.OceanChatbot(app.DATA_FILE).fish)


def deterministic_issues(question, response):
    """High-confidence checks that do not depend on an AI opinion."""
    issues = []
    reply = str(response.get("reply", "")).strip()
    result = response.get("result") or {}
    items = result.get("items") or response.get("items") or []
    if not reply:
        issues.append("답변이 비어 있습니다.")
    for word in ("총점수", "대표총점", "기타어종", "처리 경로:"):
        if word in reply:
            issues.append(f"사용자에게 보여주면 안 되는 내부 표현 ‘{word}’이 노출됐습니다.")
    unrelated = re.search(r"관광|여행|가족|랜드마크|해양\s*스포츠|힐링|산책|해수욕", question)
    fishing = re.search(r"낚시|어종|조과|출조|잡기|잡을|바다낚시|(?:예보)?지수", question)
    if unrelated and not fishing and items:
        issues.append("낚시와 무관한 질문에 낚시지수 데이터 목록을 반환했습니다.")
    if re.search(r"장비|준비물|낚싯대|미끼", question) and re.search(r"요청하신\s*어종", reply):
        issues.append("낚시 장비 질문을 어종 질문으로 잘못 해석했습니다.")
    if re.search(r"요청하신\s*어종", reply) and not any(fish in question for fish in KNOWN_FISH):
        if not re.search(r"어종|물고기|잡기|잡을|낚기", question):
            issues.append("어종을 묻지 않은 질문을 지원하지 않는 어종 질문으로 잘못 해석했습니다.")
    if result.get("kind") in {"unsupported", "clarification", "conversation", "explain"} and items:
        issues.append("설명이나 지원 범위 안내만 해야 하는 답변에 CSV 결과 행을 함께 반환했습니다.")
    return issues


def judge_prompt(history, question, response):
    history_text = "\n".join(
        f"사용자: {turn['question']}\n챗봇: {turn['answer'][:1200]}" for turn in history[-3:]
    ) or "이전 대화 없음"
    reply = str(response.get("reply", ""))
    item_count = len((response.get("result") or {}).get("items") or response.get("items") or [])
    return f"""당신은 해양예보 챗봇의 독립적인 품질 검토자입니다.

서비스 범위:
- CSV에 있는 지역별 날짜·시간·어종·바다낚시지수
- 풍속·파고·수온·기온·유속
- 사용자가 '날씨'라고 말하면 위 해양 환경 예보를 뜻하므로 지원 범위 안이다
- 지수 등급과 지원 어종의 공식 설명

서비스 범위 밖:
- 관광, 여행, 숙박, 맛집, 교통, 장비, 실제 조황·어획량
- CSV에 없는 지역·어종·정보

판정 기준:
1. 질문 의도를 정확히 이해했는가
2. 지원 범위 밖이면 정확한 이유로 안내했는가
3. 엉뚱한 낚시 데이터나 이전 질문의 조건을 재사용하지 않았는가
4. 환경·날씨 조회에 필요한 지역이나 날짜가 빠졌다면 확인 질문을 하는 것이 올바르다
5. '그 지역'처럼 가리키는 대상이 앞 대화에 실제로 없으면 지역을 다시 묻는 것이 올바르다
6. 답변이 질문에 직접적이고 자연스러운가

이전 대화:
{history_text}

현재 질문:
{question}

실제 답변:
{reply[:8000]}

반환된 데이터 행 수: {item_count}

JSON 객체만 출력하세요:
{{
  "verdict": "pass 또는 fail 또는 review",
  "reason": "사용자가 이해하기 쉬운 한국어 판정 이유",
  "ideal_answer": "문제가 있을 때 바람직한 답변 예시, 정상이면 빈 문자열",
  "confidence": 0부터 100 사이 정수,
  "issue_type": "정상이면 없음, 아니면 의도 오해/지원 범위/문맥 오류/사실 오류/답변 품질 중 하나"
}}
"""


def ai_judge(client, history, question, response):
    try:
        raw = client.chat(
            "질문과 답변의 의미를 비교하는 엄격한 QA 검토자입니다. 근거가 부족하면 review로 판정하세요.",
            judge_prompt(history, question, response),
            json_mode=True,
            max_tokens=700,
            temperature=0.1,
        )
        data = parse_json(raw)
        if not isinstance(data, dict):
            raise ValueError("judge response is not an object")
        verdict = str(data.get("verdict", "review")).lower()
        if verdict not in STATUS_LABELS:
            verdict = "review"
        try:
            confidence = max(0, min(100, int(data.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0
        return {
            "verdict": verdict,
            "reason": str(data.get("reason") or "AI 검토 이유가 비어 있습니다."),
            "ideal_answer": str(data.get("ideal_answer") or ""),
            "confidence": confidence,
            "issue_type": str(data.get("issue_type") or "알 수 없음"),
        }
    except Exception as exc:
        return {
            "verdict": "review",
            "reason": f"AI 검토 결과를 읽지 못했습니다: {exc}",
            "ideal_answer": "",
            "confidence": 0,
            "issue_type": "검토 오류",
        }


def combine_judgement(ai_result, rule_issues):
    if rule_issues:
        return {
            "status": "fail",
            "reason": " ".join(rule_issues),
            "ideal_answer": ai_result.get("ideal_answer", ""),
            "confidence": 100,
            "issue_type": "규칙 검사",
            "sources": ["Python 규칙", "AI 검토"],
        }
    verdict = ai_result["verdict"]
    confidence = ai_result["confidence"]
    status = verdict if confidence >= 70 else "review"
    return {
        "status": status,
        "reason": ai_result["reason"],
        "ideal_answer": ai_result["ideal_answer"],
        "confidence": confidence,
        "issue_type": ai_result["issue_type"],
        "sources": ["AI 검토"],
    }


def execute(bot, judge_client, cases):
    records = []
    total = sum(len(case["turns"]) for case in cases)
    completed = 0
    for case_number, case in enumerate(cases, 1):
        context = {}
        history = []
        turns = []
        for turn_number, generated in enumerate(case["turns"], 1):
            question = generated["question"]
            started = time.monotonic()
            try:
                response = bot.answer(question, context=context)
                context = response.get("context") or {}
                rule_issues = deterministic_issues(question, response)
                ai_result = ai_judge(judge_client, history, question, response)
                judgement = combine_judgement(ai_result, rule_issues)
            except Exception as exc:
                response = {"reply": "", "result": {}}
                judgement = {
                    "status": "review", "reason": f"테스트 실행 중 오류가 발생했습니다: {exc}",
                    "ideal_answer": "", "confidence": 0, "issue_type": "실행 오류",
                    "sources": ["실행기"],
                }
            turns.append({
                "number": turn_number,
                "question": question,
                "answer": response.get("reply", ""),
                "judgement": judgement,
                "seconds": round(time.monotonic() - started, 3),
                "technical": {
                    "engine": response.get("engine"),
                    "result_kind": (response.get("result") or {}).get("kind"),
                    "item_count": len((response.get("result") or {}).get("items") or response.get("items") or []),
                },
            })
            history.append({"question": question, "answer": str(response.get("reply", ""))})
            completed += 1
            print(f"[검사] {completed}/{total}개 질문", file=sys.stderr, flush=True)
        records.append({"id": f"대화-{case_number:04d}", "category": case["category"], "turns": turns})
    return records


def summary_for(records):
    counts = {"pass": 0, "fail": 0, "review": 0}
    for record in records:
        for turn in record["turns"]:
            counts[turn["judgement"]["status"]] += 1
    return {
        "total_questions": sum(counts.values()),
        "normal": counts["pass"],
        "problems": counts["fail"],
        "needs_review": counts["review"],
        "conversations": len(records),
    }


def write_reports(out, cases, records, metadata):
    out.mkdir(parents=True, exist_ok=True)
    summary = {**metadata, **summary_for(records)}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "questions.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "results.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    text = [
        "해양예보 챗봇 자동 QA 결과",
        "=" * 40,
        f"연속 대화: {summary['conversations']}개",
        f"전체 질문: {summary['total_questions']}개",
        f"정상: {summary['normal']}개",
        f"문제 있음: {summary['problems']}개",
        f"확인 필요: {summary['needs_review']}개",
        "",
    ]
    ordered = ["fail", "review", "pass"]
    for status in ordered:
        selected = [(record, turn) for record in records for turn in record["turns"]
                    if turn["judgement"]["status"] == status]
        if not selected:
            continue
        text.append(f"[{STATUS_LABELS[status]}] {len(selected)}개")
        text.append("-" * 40)
        for record, turn in selected:
            judge = turn["judgement"]
            text.extend([
                f"대화: {record['id']} / {turn['number']}턴",
                f"질문: {turn['question']}",
                f"실제 답변: {turn['answer']}",
                f"판정: {STATUS_LABELS[status]}",
                f"이유: {judge['reason']}",
                f"AI 확신도: {judge['confidence']}%",
            ])
            if judge["ideal_answer"]:
                text.append(f"바람직한 답변: {judge['ideal_answer']}")
            text.append("")
    (out / "report.txt").write_text("\n".join(text), encoding="utf-8")

    styles = """body{font-family:system-ui,sans-serif;max-width:1100px;margin:32px auto;padding:0 16px}
.summary{display:flex;gap:12px;flex-wrap:wrap}.badge{padding:12px 18px;border-radius:10px;background:#f3f4f6}
details{margin:10px 0;padding:12px;border:1px solid #ddd;border-radius:10px}
.fail{background:#fff1f1}.review{background:#fff9e8}.pass{background:#effaf2}
pre{white-space:pre-wrap;background:white;padding:12px;border-radius:8px}.reason{font-weight:600}"""
    page = [
        "<!doctype html><meta charset='utf-8'><title>챗봇 자동 QA</title>",
        f"<style>{styles}</style><h1>해양예보 챗봇 자동 QA</h1>",
        "<div class='summary'>",
        f"<div class='badge'>대화 {summary['conversations']}</div>",
        f"<div class='badge'>질문 {summary['total_questions']}</div>",
        f"<div class='badge'>정상 {summary['normal']}</div>",
        f"<div class='badge'>문제 있음 {summary['problems']}</div>",
        f"<div class='badge'>확인 필요 {summary['needs_review']}</div></div>",
    ]
    for status in ordered:
        page.append(f"<h2>{STATUS_LABELS[status]}</h2>")
        for record in records:
            for turn in record["turns"]:
                judge = turn["judgement"]
                if judge["status"] != status:
                    continue
                page.append(
                    f"<details class='{status}'><summary>{html.escape(turn['question'])}</summary>"
                    f"<h3>실제 답변</h3><pre>{html.escape(turn['answer'])}</pre>"
                    f"<p class='reason'>판정 이유: {html.escape(judge['reason'])}</p>"
                    f"<p>AI 확신도: {judge['confidence']}%</p>"
                )
                if judge["ideal_answer"]:
                    page.append(f"<h3>바람직한 답변</h3><pre>{html.escape(judge['ideal_answer'])}</pre>")
                page.append("</details>")
    (out / "report.html").write_text("".join(page), encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="사람이 읽기 쉬운 해양예보 챗봇 자동 QA")
    parser.add_argument("--generate", type=int, default=10, help="생성할 연속 대화 수")
    parser.add_argument("--categories", default="irrelevant,typo,unsupported,context_confusion")
    parser.add_argument("--max-attempts", type=int, default=0)
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    categories = [value.strip() for value in args.categories.split(",") if value.strip()]
    if not categories or set(categories) - CATEGORIES or not 1 <= args.generate <= 200:
        parser.error("카테고리 또는 generate 값이 올바르지 않습니다")
    client = app.OllamaClient(app.OLLAMA_URL, app.OLLAMA_MODEL, app.LLM_TIMEOUT)
    status = client.status(refresh=True)
    if not status.get("available"):
        print("Ollama 모델을 찾을 수 없습니다:", status, file=sys.stderr)
        return 2
    started = datetime.now(timezone.utc)
    cases = generate(client, categories, args.generate, args.max_attempts or None)
    records = execute(app.HybridOceanChatbot(app.DATA_FILE), client, cases)
    out = Path(args.output) if args.output else ROOT / "reports" / started.strftime("%Y%m%d_%H%M%S")
    summary = write_reports(out, cases, records, {
        "started": started.isoformat(), "model": app.OLLAMA_MODEL, "categories": categories,
    })
    print(json.dumps({**summary, "report": str(out / "report.txt")}, ensure_ascii=False, indent=2))
    return 1 if summary["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
