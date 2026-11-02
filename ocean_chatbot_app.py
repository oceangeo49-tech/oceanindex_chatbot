#!/usr/bin/env python3
"""Offline marine forecast chatbot prototype.

Run:
    python3 ocean_chatbot_app.py

Open:
    http://127.0.0.1:8000
"""

from __future__ import annotations

from ocean_query_policy import refine_plan, preflight, normalize, dates_in

import csv
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "SFEQ_INDEX_SERVICE_20250616.csv"
UI_FILE = BASE_DIR / "ocean_chatbot.html"
GUIDE_FILE = BASE_DIR / "ocean_index_guide.json"
OLLAMA_URL = os.environ.get("OCEAN_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OCEAN_LLM_MODEL", "qwen3:8b")
LLM_TIMEOUT = float(os.environ.get("OCEAN_LLM_TIMEOUT", "90"))


INDEX_ORDER = {
    "매우나쁨": 0,
    "나쁨": 1,
    "보통": 2,
    "좋음": 3,
    "매우좋음": 4,
}

METRIC_COLUMNS = {
    "파고": ("평균파고", "파고점수", "lower"),
    "바람": ("평균풍속", "풍속점수", "lower"),
    "풍속": ("평균풍속", "풍속점수", "lower"),
    "수온": ("평균수온", "수온점수", "higher"),
    "기온": ("평균기온", "기온점수", "higher"),
    "유속": ("평균유속", "유속점수", "lower"),
    "총점": ("총점수", "총점수", "higher"),
    "점수": ("총점수", "총점수", "higher"),
    "예보지수": ("예보지수", "예보지수", "higher"),
}

DETAIL_COLUMNS = [
    "생산일",
    "권역",
    "지역",
    "예측일자",
    "시간",
    "어종",
    "어종타입",
    "물때",
    "물때점수",
    "평균파고",
    "평균수온",
    "평균기온",
    "평균풍속",
    "평균유속",
    "총점수",
    "예보지수",
]


def _to_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return None


def _parse_ymd(value: str) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except Exception:
        return None


def _fmt_ymd(value: date) -> str:
    return value.strftime("%Y-%m-%d")


def _normalize_text(text: str) -> str:
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    return text


def good_fishing_levels(question: str) -> List[str]:
    """Qualitative fishing suitability is a grade constraint, not just a sort."""
    text = re.sub(r"\s+", "", question)
    if any(word in text for word in ("뜻", "의미", "정의", "특징", "계산", "산출", "좋지않", "안좋", "맛집", "숙박", "어획량")):
        return []
    # "좋은 지역" alone is not a fishing request. Require an explicit
    # fishing context so unrelated questions such as "싸움하기 좋은 지역"
    # cannot fall through to the marine recommendation query.
    if not re.search(r"낚시|어종|조과|출조|잡기|잡을|바다낚시|지수", text):
        return []
    if not (re.search(r"(?:좋은|괜찮은)(?:지역|곳|해역|지점)", text)
            or re.search(r"낚시(?:하기|에|가)?.*?(?:좋은|괜찮은|좋음)", text)):
        return []
    return ["매우좋음"] if re.search(r"매우좋(?:은|음)", text) else ["좋음", "매우좋음"]


@dataclass
class QuerySlots:
    raw: str
    date_range: Optional[Tuple[date, date]] = None
    time: Optional[str] = None
    regions: Optional[List[str]] = None
    fish: Optional[List[str]] = None
    metric: Optional[str] = None
    score_metric: Optional[str] = None
    orientation: Optional[str] = None
    threshold: Optional[Tuple[str, float]] = None
    group_by: Optional[str] = None
    intent: str = "detail"


class OceanChatbot:
    def __init__(self, data_file: Path):
        self.data_file = data_file
        self.rows = self._load_rows(data_file)
        self.base_date = self._compute_base_date(self.rows)
        self.regions = sorted({row["지역"] for row in self.rows}, key=len, reverse=True)
        self.fish = sorted({row["어종"] for row in self.rows}, key=len, reverse=True)
        self.summary = self._build_summary(self.rows)

    @staticmethod
    def _load_rows(path: Path) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        with path.open("r", encoding="cp949", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                parsed = dict(row)
                for key, value in row.items():
                    if key in {"생산일", "예측일자"}:
                        parsed[f"__{key}_date"] = _parse_ymd(value)
                    elif key in {
                        "물때점수",
                        "파고점수",
                        "수온점수",
                        "기온점수",
                        "풍속점수",
                        "유속점수",
                        "특보점수",
                        "총점수",
                        "평균파고",
                        "최소파고",
                        "최대파고",
                        "최소수온",
                        "평균수온",
                        "최대수온",
                        "최저기온",
                        "평균기온",
                        "최대기온",
                        "최소풍속",
                        "평균풍속",
                        "최대풍속",
                        "최소유속",
                        "평균유속",
                        "최대유속",
                    }:
                        parsed[f"__{key}_num"] = _to_float(value)
                rows.append(parsed)
        return rows

    @staticmethod
    def _compute_base_date(rows: Sequence[Dict[str, Any]]) -> date:
        dates = [row.get("__생산일_date") or row.get("__예측일자_date") for row in rows]
        dates = [d for d in dates if d is not None]
        return max(dates) if dates else date.today()

    @staticmethod
    def _build_summary(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        region_counter = Counter(row["지역"] for row in rows)
        fish_counter = Counter(row["어종"] for row in rows)
        time_counter = Counter(row["시간"] for row in rows)
        date_counter = Counter(row["예측일자"] for row in rows)
        return {
            "rows": len(rows),
            "regions": len(region_counter),
            "fish": len(fish_counter),
            "dates": len(date_counter),
            "time_slots": dict(time_counter),
            "base_date": None,
            "top_regions": region_counter.most_common(5),
            "top_fish": fish_counter.most_common(5),
        }

    def parse_slots(self, text: str) -> QuerySlots:
        raw = _normalize_text(text)
        slots = QuerySlots(raw=raw)

        slots.date_range = self._parse_date_range(raw)
        slots.time = self._parse_time(raw)
        slots.regions = self._parse_regions(raw)
        slots.fish = self._parse_fish(raw)
        slots.metric, slots.score_metric, slots.orientation = self._parse_metric(raw)
        slots.threshold = self._parse_threshold(raw, slots.metric)
        slots.group_by, slots.intent = self._infer_group_and_intent(raw, slots)
        return slots

    def _parse_date_range(self, text: str) -> Optional[Tuple[date, date]]:
        base = self.base_date

        explicit = self._parse_explicit_date(text)
        if explicit:
            return explicit

        keyword_offsets = {
            "오늘": 0,
            "금일": 0,
            "내일": 1,
            "모레": 2,
            "글피": 3,
            "어제": -1,
        }
        for key, offset in keyword_offsets.items():
            if key in text:
                target = base + timedelta(days=offset)
                return target, target

        if "이번주" in text or "이번 주" in text:
            return base, base + timedelta(days=6)

        if "주말" in text:
            week_end = base + timedelta(days=6)
            d = base
            weekend_days = []
            while d <= week_end:
                if d.weekday() >= 5:
                    weekend_days.append(d)
                d += timedelta(days=1)
            if weekend_days:
                return weekend_days[0], weekend_days[-1]

        if "이번달" in text or "이번 달" in text:
            first = base.replace(day=1)
            if first.month == 12:
                next_month = first.replace(year=first.year + 1, month=1, day=1)
            else:
                next_month = first.replace(month=first.month + 1, day=1)
            return first, next_month - timedelta(days=1)

        return None

    @staticmethod
    def _parse_explicit_date(text: str) -> Optional[Tuple[date, date]]:
        patterns = [
            r"(20\d{2})[.\-/년 ]?(\d{1,2})[.\-/월 ]?(\d{1,2})일?",
            r"(20\d{6})",
        ]
        for pattern in patterns:
            m = re.search(pattern, text)
            if not m:
                continue
            if len(m.groups()) == 1:
                ymd = m.group(1)
                try:
                    d = datetime.strptime(ymd, "%Y%m%d").date()
                    return d, d
                except Exception:
                    continue
            y, mth, d = m.groups()
            try:
                parsed = date(int(y), int(mth), int(d))
                return parsed, parsed
            except Exception:
                continue
        return None

    @staticmethod
    def _parse_time(text: str) -> Optional[str]:
        if any(token in text for token in ["오전", "아침", "AM", "am"]):
            return "AM"
        if any(token in text for token in ["오후", "PM", "pm", "점심", "저녁"]):
            return "PM"
        if any(token in text for token in ["종일", "하루", "하루종일", "전체", "전일", "하루 전체", "all day"]):
            return "DY"
        return None

    def _parse_regions(self, text: str) -> Optional[List[str]]:
        # Longest match at each position: 방포항 alone is not 포항, but an explicit
        # "포항과 방포항" must preserve both destinations.
        pattern = "|".join(re.escape(region) for region in sorted(self.regions, key=len, reverse=True))
        return list(dict.fromkeys(re.findall(pattern, text))) or None

    def _parse_fish(self, text: str) -> Optional[List[str]]:
        matches = [fish for fish in self.fish if fish in text]
        if matches:
            matches = self._prefer_specific(matches)
            return matches
        return None

    @staticmethod
    def _prefer_specific(items: Sequence[str]) -> List[str]:
        items = sorted(set(items), key=len, reverse=True)
        filtered: List[str] = []
        for item in items:
            if any(item != other and item in other for other in items):
                continue
            filtered.append(item)
        return filtered or list(dict.fromkeys(items))

    @staticmethod
    def _parse_metric(text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        for keyword, (metric_col, score_col, orientation) in METRIC_COLUMNS.items():
            if keyword in text:
                return metric_col, score_col, orientation
        return None, None, None

    @staticmethod
    def _parse_threshold(text: str, metric_col: Optional[str]) -> Optional[Tuple[str, float]]:
        if not metric_col:
            return None
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(m/s|미터|미터?|\bM\b|m|도|℃)?\s*(이하|미만|이상|초과|내|이내)", text)
        if not m:
            return None
        value = float(m.group(1))
        op_word = m.group(3)
        op_map = {
            "이하": "<=",
            "미만": "<",
            "이상": ">=",
            "초과": ">",
            "내": "<=",
            "이내": "<=",
        }
        return op_map.get(op_word, "<="), value

    def _infer_group_and_intent(self, text: str, slots: QuerySlots) -> Tuple[Optional[str], str]:
        # Intent classification is intentionally simple and deterministic.
        if any(word in text for word in ["비교", "순위", "랭킹", "top", "가장", "제일", "최고", "최저"]):
            intent = "ranking"
        elif any(word in text for word in ["어때", "괜찮", "좋아", "추천", "알려줘", "봐줘"]):
            intent = "recommend"
        else:
            intent = "detail"

        group_by: Optional[str] = None
        if slots.date_range and any(word in text for word in ["날짜", "언제", "언제가", "이번주", "이번 주", "주말", "요일"]):
            if not slots.regions and not slots.fish:
                group_by = "date"

        if group_by is None and any(word in text for word in ["지역", "어디", "곳", "지점"]) and not slots.regions:
            group_by = "region"

        if group_by is None and any(word in text for word in ["어종", "물고기", "어떤 어종"]) and not slots.fish:
            group_by = "fish"

        if group_by is None and intent == "ranking" and not slots.regions:
            group_by = "region"

        return group_by, intent

    def _filter_rows(self, slots: QuerySlots) -> List[Dict[str, Any]]:
        rows = self.rows

        if slots.date_range:
            start, end = slots.date_range
            rows = [r for r in rows if start <= (r.get("__예측일자_date") or start) <= end]
        if slots.time:
            rows = [r for r in rows if r["시간"] == slots.time]
        if slots.regions:
            region_set = set(slots.regions)
            rows = [r for r in rows if any(region in r["지역"] for region in region_set)]
        if slots.fish:
            fish_set = set(slots.fish)
            rows = [r for r in rows if any(fish in r["어종"] for fish in fish_set)]

        if slots.threshold and slots.metric:
            op, threshold = slots.threshold
            filtered: List[Dict[str, Any]] = []
            for row in rows:
                value = self._get_metric_value(row, slots.metric)
                if value is None:
                    continue
                if op == "<=" and value <= threshold:
                    filtered.append(row)
                elif op == "<" and value < threshold:
                    filtered.append(row)
                elif op == ">=" and value >= threshold:
                    filtered.append(row)
                elif op == ">" and value > threshold:
                    filtered.append(row)
            rows = filtered

        return rows

    @staticmethod
    def _get_metric_value(row: Dict[str, Any], metric_col: str) -> Optional[float]:
        return row.get(f"__{metric_col}_num")

    @staticmethod
    def _get_score_value(row: Dict[str, Any], score_col: str) -> Optional[float]:
        return row.get(f"__{score_col}_num")

    def answer(self, text: str) -> Dict[str, Any]:
        slots = self.parse_slots(text)
        rows = self._filter_rows(slots)

        if not rows:
            return {
                "ok": True,
                "reply": self._no_match_reply(slots),
                "slots": self._serialize_slots(slots),
                "items": [],
            }

        if slots.group_by:
            items = self._group_and_rank(rows, slots)
            reply = self._format_ranking_reply(slots, rows, items)
        else:
            items = self._detail_rows(rows, slots)
            reply = self._format_detail_reply(slots, rows, items)

        return {
            "ok": True,
            "reply": reply,
            "slots": self._serialize_slots(slots),
            "items": items,
        }

    @staticmethod
    def _serialize_slots(slots: QuerySlots) -> Dict[str, Any]:
        payload = {
            "raw": slots.raw,
            "date_range": None,
            "time": slots.time,
            "regions": slots.regions,
            "fish": slots.fish,
            "metric": slots.metric,
            "score_metric": slots.score_metric,
            "orientation": slots.orientation,
            "threshold": slots.threshold,
            "group_by": slots.group_by,
            "intent": slots.intent,
        }
        if slots.date_range:
            payload["date_range"] = [_fmt_ymd(slots.date_range[0]), _fmt_ymd(slots.date_range[1])]
        return payload

    def _preferred_sort_column(self, slots: QuerySlots) -> Tuple[str, bool]:
        # bool is reverse sort
        if slots.metric and slots.threshold:
            return slots.metric, slots.orientation != "lower"

        if slots.metric:
            if slots.intent == "recommend" or any(word in slots.raw for word in ["좋", "추천", "괜찮", "유리", "괜찬"]):
                return slots.score_metric or "총점수", True
            if slots.orientation == "lower":
                return slots.metric, False
            return slots.metric, True

        return "총점수", True

    def _ranking_base_value(self, row: Dict[str, Any], column: str) -> Any:
        if column == "예보지수":
            return INDEX_ORDER.get(row.get("예보지수"), -1)
        if column in {"총점수", "평균파고", "평균수온", "평균기온", "평균풍속", "평균유속"}:
            return row.get(f"__{column}_num")
        score_col = column
        return row.get(f"__{score_col}_num")

    def _group_and_rank(self, rows: Sequence[Dict[str, Any]], slots: QuerySlots) -> List[Dict[str, Any]]:
        group_by = slots.group_by or "region"
        sort_column, reverse = self._preferred_sort_column(slots)
        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if group_by == "date":
                key = row["예측일자"]
            elif group_by == "fish":
                key = row["어종"]
            else:
                key = row["지역"]
            groups[key].append(row)

        items: List[Dict[str, Any]] = []
        for key, group_rows in groups.items():
            metric_values = [self._ranking_base_value(r, sort_column) for r in group_rows]
            metric_values = [v for v in metric_values if isinstance(v, (int, float))]
            if not metric_values:
                continue
            score_avg = mean(metric_values)
            best_row = self._pick_best_row(group_rows, sort_column, reverse)
            items.append(
                {
                    "group": key,
                    "count": len(group_rows),
                    "value": round(score_avg, 2),
                    "sort_column": sort_column,
                    "best_row": self._compact_row(best_row) if best_row else None,
                }
            )

        items.sort(key=lambda item: item["value"], reverse=reverse)
        return items[:5]

    def _pick_best_row(self, rows: Sequence[Dict[str, Any]], sort_column: str, reverse: bool) -> Optional[Dict[str, Any]]:
        def keyfunc(row: Dict[str, Any]) -> Tuple[int, float]:
            if sort_column == "예보지수":
                return INDEX_ORDER.get(row.get("예보지수"), -1), row.get("__총점수_num") or -1
            if sort_column in {"총점수", "평균파고", "평균수온", "평균기온", "평균풍속", "평균유속"}:
                value = row.get(f"__{sort_column}_num")
                return int(value * 1000) if value is not None else -1, row.get("__총점수_num") or -1
            score = row.get(f"__{sort_column}_num")
            return int(score * 1000) if score is not None else -1, row.get("__총점수_num") or -1

        valid = [r for r in rows if self._ranking_base_value(r, sort_column) is not None]
        if not valid:
            return None
        return max(valid, key=keyfunc) if reverse else min(valid, key=keyfunc)

    def _detail_rows(self, rows: Sequence[Dict[str, Any]], slots: QuerySlots) -> List[Dict[str, Any]]:
        sort_column, reverse = self._preferred_sort_column(slots)
        ranked = sorted(
            rows,
            key=lambda row: self._ranking_base_value(row, sort_column) if self._ranking_base_value(row, sort_column) is not None else -999999,
            reverse=reverse,
        )
        return [self._compact_row(row) for row in ranked[:5]]

    def _compact_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        for col in DETAIL_COLUMNS:
            value = row.get(col)
            if col in {"생산일", "예측일자"} and value:
                payload[col] = value
            elif isinstance(value, str) and value != "":
                payload[col] = value
            elif isinstance(value, (int, float)):
                payload[col] = value
            else:
                num_value = row.get(f"__{col}_num")
                if num_value is not None:
                    payload[col] = round(num_value, 2)
        return payload

    def _format_ranking_reply(self, slots: QuerySlots, rows: Sequence[Dict[str, Any]], items: Sequence[Dict[str, Any]]) -> str:
        if not items:
            return self._no_match_reply(slots)

        date_text = self._describe_date_range(slots.date_range)
        parts = [f"기준일은 {self._base_date_text()}입니다."]
        if date_text:
            parts.append(date_text)
        if slots.time:
            parts.append(f"{slots.time} 조건")
        if slots.fish:
            parts.append(f"어종: {', '.join(slots.fish)}")
        if slots.regions:
            parts.append(f"지역: {', '.join(slots.regions)}")

        metric_name = self._display_metric_name(items[0]["sort_column"])
        group_name = {"region": "지역", "date": "날짜", "fish": "어종"}.get(slots.group_by or "region", "지역")

        lines = [f"{' / '.join(parts)}에서 {group_name} 기준 상위 결과입니다."]
        for idx, item in enumerate(items, 1):
            group = self._pretty_group_value(item["group"], slots.group_by)
            value = item["value"]
            best_row = item.get("best_row") or {}
            extra = []
            if best_row.get("예보지수"):
                extra.append(f"예보지수 {best_row['예보지수']}")
            if best_row.get("총점수") is not None:
                extra.append(f"총점수 {best_row['총점수']}")
            if best_row.get("시간"):
                extra.append(f"{best_row['시간']}")
            lines.append(f"{idx}. {group} - {metric_name} {value:.2f} ({', '.join(extra)})")

        if slots.metric and slots.threshold:
            op, threshold = slots.threshold
            lines.append(f"조건: {metric_name} {op} {threshold:g}")

        return "\n".join(lines)

    def _format_detail_reply(self, slots: QuerySlots, rows: Sequence[Dict[str, Any]], items: Sequence[Dict[str, Any]]) -> str:
        date_text = self._describe_date_range(slots.date_range)
        if len(rows) == 1:
            row = rows[0]
            head = [f"딱 1건을 찾았습니다. {self._base_date_text()} 기준 데이터입니다."]
            if date_text:
                head.append(date_text)
            if slots.time:
                head.append(f"{slots.time} 조건")
            if slots.regions:
                head.append(f"지역: {', '.join(slots.regions)}")
            if slots.fish:
                head.append(f"어종: {', '.join(slots.fish)}")
            lines = [" / ".join(head)]
            lines.extend(self._format_row_sentence(row))
            return "\n".join(lines)

        lines = [f"{len(rows)}건을 찾았습니다."]
        if date_text:
            lines.append(date_text)
        if slots.regions:
            lines.append(f"지역: {', '.join(slots.regions)}")
        if slots.fish:
            lines.append(f"어종: {', '.join(slots.fish)}")
        lines.append("상위 5개 결과:")
        for idx, item in enumerate(items, 1):
            lines.append(f"{idx}. {self._compact_row_text(item)}")
        return "\n".join(lines)

    @staticmethod
    def _format_row_sentence(row: Dict[str, Any]) -> List[str]:
        return [
            f"- 지역: {row.get('지역')} ({row.get('권역')})",
            f"- 일시: {OceanChatbot._pretty_ymd(row.get('예측일자'))} {row.get('시간')}",
            f"- 어종: {row.get('어종')} / 어종타입: {row.get('어종타입')}",
            f"- 물때: {row.get('물때')} ({row.get('물때점수')})",
            f"- 평균파고: {row.get('평균파고')}m, 평균수온: {row.get('평균수온')}℃, 평균기온: {row.get('평균기온')}℃",
            f"- 평균풍속: {row.get('평균풍속')}m/s, 평균유속: {row.get('평균유속')}m/s",
            f"- 총점수: {row.get('총점수')}, 예보지수: {row.get('예보지수')}",
        ]

    @staticmethod
    def _compact_row_text(item: Dict[str, Any]) -> str:
        parts = []
        for key in ["지역", "어종", "예측일자", "시간", "총점수", "예보지수"]:
            if key in item:
                value = item[key]
                if key == "예측일자":
                    value = OceanChatbot._pretty_ymd(str(value))
                parts.append(f"{key}:{value}")
        return ", ".join(parts)

    @staticmethod
    def _describe_date_range(date_range: Optional[Tuple[date, date]]) -> str:
        if not date_range:
            return ""
        start, end = date_range
        if start == end:
            return f"날짜: {_fmt_ymd(start)}"
        return f"날짜 범위: {_fmt_ymd(start)} ~ {_fmt_ymd(end)}"

    def _human_metric_name(self, slots: QuerySlots) -> str:
        if slots.score_metric:
            mapping = {
                "파고점수": "파고점수",
                "풍속점수": "풍속점수",
                "수온점수": "수온점수",
                "기온점수": "기온점수",
                "유속점수": "유속점수",
                "총점수": "총점수",
                "예보지수": "예보지수",
            }
            return mapping.get(slots.score_metric, slots.score_metric)
        return slots.metric or "총점수"

    def _base_date_text(self) -> str:
        return _fmt_ymd(self.base_date)

    @staticmethod
    def _display_metric_name(column: str) -> str:
        mapping = {
            "평균파고": "평균파고",
            "평균수온": "평균수온",
            "평균기온": "평균기온",
            "평균풍속": "평균풍속",
            "평균유속": "평균유속",
            "파고점수": "파고점수",
            "수온점수": "수온점수",
            "기온점수": "기온점수",
            "풍속점수": "풍속점수",
            "유속점수": "유속점수",
            "총점수": "총점수",
            "예보지수": "예보지수",
        }
        return mapping.get(column, column)

    @staticmethod
    def _pretty_ymd(value: Optional[str]) -> str:
        if not value:
            return "-"
        if re.fullmatch(r"\d{8}", str(value)):
            return f"{value[:4]}-{value[4:6]}-{value[6:]}"
        return str(value)

    @staticmethod
    def _pretty_group_value(value: Any, group_by: Optional[str]) -> str:
        if group_by == "date":
            return OceanChatbot._pretty_ymd(str(value))
        return str(value)

    def _no_match_reply(self, slots: QuerySlots) -> str:
        parts = [f"조건에 맞는 결과를 찾지 못했습니다. {self._base_date_text()} 기준으로 다시 조회해볼게요."]
        recognized = []
        if slots.date_range:
            recognized.append(self._describe_date_range(slots.date_range))
        if slots.time:
            recognized.append(f"시간: {slots.time}")
        if slots.regions:
            recognized.append(f"지역: {', '.join(slots.regions)}")
        if slots.fish:
            recognized.append(f"어종: {', '.join(slots.fish)}")
        if slots.metric:
            recognized.append(f"지표: {slots.metric}")
        if recognized:
            parts.append("인식한 조건: " + " / ".join(recognized))
        parts.append("예: '내일 오후 바람 가장 약한 지역', '가거도 감성돔 내일 예보', '이번 주 가장 좋은 날짜'")
        return "\n".join(parts)

    def meta(self) -> Dict[str, Any]:
        return {
            "rows": len(self.rows),
            "regions": len(self.regions),
            "fish": len(self.fish),
            "base_date": _fmt_ymd(self.base_date),
            "time_slots": Counter(row["시간"] for row in self.rows),
            "top_regions": Counter(row["지역"] for row in self.rows).most_common(8),
            "top_fish": Counter(row["어종"] for row in self.rows).most_common(8),
        }


QUERY_FIELDS = {
    "date": "예측일자",
    "time": "시간",
    "area": "권역",
    "region": "지역",
    "fish": "어종",
    "fish_type": "어종타입",
    "tide": "물때",
    "forecast_index": "예보지수",
}

QUERY_METRICS = {
    "물때점수",
    "최소파고",
    "평균파고",
    "최대파고",
    "파고점수",
    "최소수온",
    "평균수온",
    "최대수온",
    "수온점수",
    "최저기온",
    "평균기온",
    "최대기온",
    "기온점수",
    "최소풍속",
    "평균풍속",
    "최대풍속",
    "풍속점수",
    "최소유속",
    "평균유속",
    "최대유속",
    "유속점수",
    "특보점수",
    "총점수",
    "예보지수",
}

ENVIRONMENT_METRIC_GROUPS = {
    "파고": ["최소파고", "평균파고", "최대파고"],
    "수온": ["최소수온", "평균수온", "최대수온"],
    "기온": ["최저기온", "평균기온", "최대기온"],
    "풍속": ["최소풍속", "평균풍속", "최대풍속"],
    "바람": ["최소풍속", "평균풍속", "최대풍속"],
    "유속": ["최소유속", "평균유속", "최대유속"],
}

ENVIRONMENT_UNITS = {
    "파고": "m",
    "수온": "℃",
    "기온": "℃",
    "풍속": "m/s",
    "유속": "m/s",
}

QUERY_OPERATIONS = {
    "distinct",
    "detail",
    "ranking",
    "count",
    "compare",
    "trend",
    "aggregate",
    "explain",
    "unsupported",
}

TERM_GLOSSARY = {
    "예보지수": (
        "지수 파일에서 해양환경 관련 점수와 함께 제공되는 최종 등급 항목입니다. "
        "값은 매우좋음, 좋음, 보통, 나쁨, 매우나쁨으로 제공됩니다. "
        "현재 CSV에는 정확한 산출 공식과 등급 경계값이 포함되어 있지 않습니다."
    ),
    "총점수": (
        "지수 파일에 수록된 종합 점수입니다. 물때, 파고, 수온, 기온, 풍속, 유속, "
        "특보 관련 점수와 함께 제공되지만 정확한 가중치와 산식은 현재 CSV만으로 확인할 수 없습니다."
    ),
    "파고": "파도의 높이이며 CSV에서는 최소파고, 평균파고, 최대파고와 파고점수를 제공합니다. 단위는 m입니다.",
    "수온": "바닷물의 온도이며 CSV에서는 최소수온, 평균수온, 최대수온과 수온점수를 제공합니다. 단위는 ℃입니다.",
    "기온": "공기의 온도이며 CSV에서는 최저기온, 평균기온, 최대기온과 기온점수를 제공합니다. 단위는 ℃입니다.",
    "풍속": "바람의 속도이며 CSV에서는 최소풍속, 평균풍속, 최대풍속과 풍속점수를 제공합니다. 단위는 m/s입니다.",
    "유속": "바닷물 흐름의 속도이며 CSV에서는 최소유속, 평균유속, 최대유속과 유속점수를 제공합니다. 단위는 m/s입니다.",
}


class OfficialGuide:
    def __init__(self, path: Path):
        self.path = path
        with path.open("r", encoding="utf-8") as handle:
            self.data = json.load(handle)

    def meta(self) -> Dict[str, Any]:
        return {
            "loaded": True,
            "file": self.path.name,
            "title": self.data.get("metadata", {}).get("title"),
            "index_levels": list(self.data.get("index_levels", {})),
            "fish": list(self.data.get("fish", {})),
        }

    def lookup(self, term: Optional[str]) -> Optional[Dict[str, Any]]:
        term = (term or "예보지수").strip()
        if term in {"예보지수", "바다낚시지수", "낚시지수", "지수"}:
            overview = self.data.get("index_overview", {})
            return {
                "category": "index_overview",
                "term": "예보지수",
                "definition": overview.get("safe_summary") or overview.get("definition"),
                "source_definition": overview.get("definition"),
                "levels": overview.get("levels", []),
                "time_standard": self.data.get("time_standard", {}),
                "safety": self.data.get("safety", []),
                "answer_policy": self.data.get("answer_policy", []),
            }
        levels = self.data.get("index_levels", {})
        if term in levels:
            item = levels[term]
            return {
                "category": "index_level",
                "term": term,
                "definition": item.get("safe_summary"),
                "safety": self.data.get("safety", []),
                "answer_policy": self.data.get("answer_policy", []),
            }
        fish = self.data.get("fish", {})
        if term in fish:
            item = fish[term]
            return {
                "category": "fish",
                "term": term,
                "definition": item.get("safe_summary"),
                "source_text": item.get("source_text"),
                "source_name": item.get("source_name", term),
                "safety": self.data.get("safety", []),
                "answer_policy": self.data.get("answer_policy", []),
            }
        return None

    def context_for_result(self, plan: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        levels = []
        for item in result.get("items", []):
            level = item.get("예보지수") or item.get("대표예보지수")
            if level and level not in levels:
                levels.append(level)
        fish_terms = [term for term in plan.get("filters", {}).get("fish", []) if term in self.data.get("fish", {})]
        return {
            "index_levels": [self.lookup(level) for level in levels if self.lookup(level)],
            "fish": [self.lookup(term) for term in fish_terms if self.lookup(term)],
            "notice": self.data.get("metadata", {}).get("notice"),
            "safety": self.data.get("safety", []),
            "answer_policy": self.data.get("answer_policy", []),
        }


class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout: float):
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self._status_cache: Tuple[float, Dict[str, Any]] = (0.0, {})

    def _request(self, path: str, payload: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="GET" if payload is None else "POST",
        )
        try:
            with urlopen(request, timeout=timeout or self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Ollama request failed: {exc}") from exc

    def status(self, refresh: bool = False) -> Dict[str, Any]:
        cached_at, cached = self._status_cache
        if not refresh and cached and time.monotonic() - cached_at < 10:
            return cached
        try:
            data = self._request("/api/tags", timeout=3)
            names = [str(item.get("name", "")) for item in data.get("models", [])]
            available = self.model in names or any(name.split(":")[0] == self.model for name in names)
            status = {"connected": True, "model": self.model, "available": available, "installed_models": names}
        except RuntimeError as exc:
            status = {"connected": False, "model": self.model, "available": False, "error": str(exc)}
        self._status_cache = (time.monotonic(), status)
        return status

    def chat(self, system: str, user: str, json_mode: bool = False, max_tokens: int = 700,
             temperature: float = 0.1) -> str:
        payload: Dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "think": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {"temperature": temperature, "num_predict": max_tokens},
            "keep_alive": "15m",
        }
        if json_mode:
            payload["format"] = "json"
        data = self._request("/api/chat", payload)
        content = data.get("message", {}).get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Ollama returned an empty response")
        return content.strip()


class LLMQueryPlanner:
    FILTER_KEYS = {"dates", "times", "areas", "regions", "fish", "fish_types", "tides", "forecast_indexes"}

    def __init__(self, client: OllamaClient, bot: OceanChatbot):
        self.client = client
        self.bot = bot

    @staticmethod
    def _extract_json(text: str) -> Dict[str, Any]:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("LLM response did not contain JSON")
            data = json.loads(text[start : end + 1])
        if not isinstance(data, dict):
            raise ValueError("Query plan must be a JSON object")
        return data

    @staticmethod
    def _as_list(value: Any) -> List[str]:
        if value is None:
            return []
        values = value if isinstance(value, list) else [value]
        return [str(item).strip() for item in values if str(item).strip()]

    def _system_prompt(self) -> str:
        dates = sorted({OceanChatbot._pretty_ymd(row["예측일자"]) for row in self.bot.rows})
        areas = sorted({row["권역"] for row in self.bot.rows})
        return f"""당신은 해양예보 CSV 질문을 실행 계획 JSON으로 바꾸는 분석기입니다.
설명이나 markdown 없이 JSON 객체만 출력하세요.

데이터 기준일: {_fmt_ymd(self.bot.base_date)}
조회 가능 날짜: {json.dumps(dates, ensure_ascii=False)}
권역: {json.dumps(areas, ensure_ascii=False)}
지역: {json.dumps(sorted(self.bot.regions), ensure_ascii=False)}
어종: {json.dumps(sorted(self.bot.fish), ensure_ascii=False)}
시간 값: ["AM", "PM", "DY"]
예보지수 값: ["매우좋음", "좋음", "보통", "나쁨", "매우나쁨"]
사용 가능 수치 지표: {json.dumps(sorted(QUERY_METRICS - {"예보지수"}), ensure_ascii=False)}

반드시 아래 키를 사용하세요.
{{
  "operation": "distinct|detail|ranking|count|compare|trend|aggregate|explain|unsupported",
  "filters": {{
    "dates": [], "times": [], "areas": [], "regions": [], "fish": [],
    "fish_types": [], "tides": [], "forecast_indexes": []
  }},
  "numeric_conditions": [{{"field":"평균파고", "operator":"<=", "value":1.0}}],
  "select": ["지역", "어종"],
  "group_by": "지역|null",
  "metric": "평균파고|null",
  "aggregation": "average|min|max|sum|count",
  "sort": "asc|desc|null",
  "limit": 5,
  "return_all": false,
  "include_guide": false,
  "term": null,
  "clarification_needed": false,
  "clarification_question": null,
  "unsupported_reason": null
}}

해석 규칙:
- 오늘/내일/모레는 기준일로부터 실제 YYYY-MM-DD 날짜로 변환합니다.
- "내일 모레"는 내일과 모레 두 날짜, "모레"와 "내일모레"는 모레 하루입니다.
- 조건이 없거나 전체를 뜻하면 배열을 비워두세요. 전체 값을 배열에 나열하지 마세요.
- 서비스 지역/제공 지역/어종 목록은 distinct입니다. "전부/모두/다"면 return_all=true입니다.
- "잡히는 지역"은 실제 어획량이 아니라 해당 어종 지수가 제공되는 지역이라는 뜻으로만 해석합니다.
- 가장 낮은/약한은 sort=asc, 가장 높은/좋은은 sort=desc입니다.
- 따뜻한 곳은 평균기온, 물이 따뜻한 곳은 평균수온, 잔잔한 곳은 평균파고를 사용합니다.
- 날짜·지역의 단순 기온/수온/파고/풍속/유속 조회는 어종과 무관한 환경 조회입니다. 어종을 select에 넣지 말고, 날짜·지역·시간과 해당 환경의 최소·평균·최대 값을 select하며 return_all=true로 합니다.
- 날짜만 있고 시간이 없으면 모든 시간대 자료를 대상으로 average 집계합니다.
- "낚시하기 좋은 지역/좋은 곳/괜찮은 지역"은 filters.forecast_indexes=["좋음","매우좋음"]으로 제한합니다. "매우 좋은"은 ["매우좋음"]만 조회합니다.
- 좋은 지역 추천은 detail, metric="총점수", sort="desc"로 조회하고 select는 예측일자, 지역, 시간, 어종, 총점수, 예보지수입니다. 다른 등급은 추천하지 않습니다.
- 추천의 기본 limit는 5이며, 전체 요청일 때만 return_all=true입니다. 조건에 맞는 자료가 없으면 필터를 풀지 않습니다.
- "가장/제일"은 ranking이며 별도 개수 표현이 없으면 limit=1입니다.
- numeric_conditions는 사용자가 숫자와 이상/이하/초과/미만을 직접 말한 경우에만 작성합니다.
- 바다낚시지수와 낚시지수는 CSV의 예보지수와 같은 조회 표현으로 취급합니다.
- "예보지수가 뭐야/뜻/의미/정의/설명"처럼 용어 자체를 묻는 경우만 explain입니다.
- 날짜·지역·어종 조건과 함께 "예보지수 알려줘/보여줘"라고 하면 detail입니다.
- 예보지수 detail에서 어종이나 시간이 생략되면 모든 어종·시간을 반환하고 select는 예측일자, 지역, 시간, 어종, 총점수, 예보지수입니다.
- "지수와 의미/뜻/설명"을 함께 요청하면 detail을 유지하고 include_guide=true로 합니다.
- 매우좋음·좋음·보통·나쁨·매우나쁨 단계의 의미나 어종의 특징·서식 경향 질문은 explain이며 term에 해당 단계나 어종을 넣습니다.
- 실제 어획량, 맛집, 교통처럼 CSV에 없는 정보는 unsupported입니다.
- 뜻이 둘 이상으로 애매하면 clarification_needed=true로 하고 짧은 확인 질문을 작성합니다.
- 동해는 동해중부·동해남부, 남해는 남해서부·남해동부, 서해(황해)는 황해중부·황해남부 권역을 함께 조회합니다. 특정 세부 권역을 명시하면 그 권역만 조회합니다.
- 없는 지역·어종·지표를 새로 만들지 마세요.

예시 1
질문: 내일 참돔 서비스 지역 중 파도가 가장 낮은 곳을 추천해줘
JSON: {{"operation":"ranking","filters":{{"dates":["{_fmt_ymd(self.bot.base_date + timedelta(days=1))}"],"times":[],"areas":[],"regions":[],"fish":["참돔"],"fish_types":[],"tides":[],"forecast_indexes":[]}},"numeric_conditions":[],"select":["지역"],"group_by":"지역","metric":"평균파고","aggregation":"average","sort":"asc","limit":1,"return_all":false,"term":null,"clarification_needed":false,"clarification_question":null,"unsupported_reason":null}}

예시 2
질문: 참돔을 서비스하는 지역 전부 알려줘
JSON: {{"operation":"distinct","filters":{{"dates":[],"times":[],"areas":[],"regions":[],"fish":["참돔"],"fish_types":[],"tides":[],"forecast_indexes":[]}},"numeric_conditions":[],"select":["지역"],"group_by":null,"metric":null,"aggregation":"count","sort":null,"limit":5,"return_all":true,"term":null,"clarification_needed":false,"clarification_question":null,"unsupported_reason":null}}

예시 3
질문: 내일 울산 바다낚시지수 알려줘
JSON: {{"operation":"detail","filters":{{"dates":["{_fmt_ymd(self.bot.base_date + timedelta(days=1))}"],"times":[],"areas":[],"regions":["울산"],"fish":[],"fish_types":[],"tides":[],"forecast_indexes":[]}},"numeric_conditions":[],"select":["예측일자","지역","시간","어종","총점수","예보지수"],"group_by":null,"metric":null,"aggregation":"average","sort":null,"limit":5,"return_all":true,"term":null,"clarification_needed":false,"clarification_question":null,"unsupported_reason":null}}

예시 4
질문: 내일 울릉도 기온 몇이야
JSON: {{"operation":"detail","filters":{{"dates":["{_fmt_ymd(self.bot.base_date + timedelta(days=1))}"],"times":[],"areas":[],"regions":["울릉도"],"fish":[],"fish_types":[],"tides":[],"forecast_indexes":[]}},"numeric_conditions":[],"select":["예측일자","지역","시간","최저기온","평균기온","최대기온"],"group_by":null,"metric":null,"aggregation":"average","sort":null,"limit":5,"return_all":true,"term":null,"clarification_needed":false,"clarification_question":null,"unsupported_reason":null}}
"""

    def parse(self, question: str) -> Dict[str, Any]:
        raw = self.client.chat(self._system_prompt(), question, json_mode=True, max_tokens=850)
        return self.validate(self._extract_json(raw), question)

    def validate(self, raw: Dict[str, Any], question: str = "") -> Dict[str, Any]:
        operation = str(raw.get("operation", "unsupported")).strip().lower()
        if operation not in QUERY_OPERATIONS:
            operation = "unsupported"

        incoming_filters = raw.get("filters") if isinstance(raw.get("filters"), dict) else {}
        filters = {key: self._as_list(incoming_filters.get(key)) for key in self.FILTER_KEYS}

        date_values = []
        for value in filters["dates"]:
            parsed = None
            for fmt in ("%Y-%m-%d", "%Y%m%d"):
                try:
                    parsed = datetime.strptime(value, fmt).date()
                    break
                except ValueError:
                    continue
            if parsed:
                date_values.append(_fmt_ymd(parsed))
        filters["dates"] = list(dict.fromkeys(date_values))
        filters["times"] = [value.upper() for value in filters["times"] if value.upper() in {"AM", "PM", "DY"}]
        filters["forecast_indexes"] = [value for value in filters["forecast_indexes"] if value in INDEX_ORDER]

        allowed_values = {
            "areas": sorted({row["권역"] for row in self.bot.rows}),
            "regions": self.bot.regions,
            "fish": self.bot.fish,
            "fish_types": sorted({row["어종타입"] for row in self.bot.rows}),
            "tides": sorted({row["물때"] for row in self.bot.rows}),
        }
        for key, allowed in allowed_values.items():
            filters[key] = [
                value for value in filters[key]
                if value in allowed or any(value in candidate or candidate in value for candidate in allowed)
            ]
            if set(filters[key]) == set(allowed):
                filters[key] = []
        # Resolve sea names from the user's words, even when the model drops the area.
        # Match specific subareas first so "동해중부" does not expand to all of 동해.
        area_mentions = set()
        remaining_area_text = question
        for area in sorted(allowed_values["areas"], key=len, reverse=True):
            if area in remaining_area_text:
                area_mentions.add(area)
                remaining_area_text = remaining_area_text.replace(area, " ")
        for sea, prefix in {"동해": "동해", "서해": "황해", "황해": "황해", "남해": "남해"}.items():
            if sea in remaining_area_text:
                area_mentions.update(area for area in allowed_values["areas"] if area.startswith(prefix))
        if area_mentions and not re.search(r"제외|빼고|말고", question):
            filters["areas"] = sorted(area_mentions)
            # A sea-wide request must not acquire an unrelated region from the model.
            explicit_regions = self.bot._parse_regions(question) or []
            filters["regions"] = explicit_regions

        if set(filters["times"]) == {"AM", "PM", "DY"}:
            filters["times"] = []

        select = [value for value in self._as_list(raw.get("select")) if value in set(QUERY_FIELDS.values()) | QUERY_METRICS]
        group_by = raw.get("group_by")
        if group_by not in set(QUERY_FIELDS.values()):
            group_by = None
        metric = raw.get("metric")
        if metric not in QUERY_METRICS:
            metric = None
        aggregation = str(raw.get("aggregation", "average")).lower()
        if aggregation not in {"average", "min", "max", "sum", "count"}:
            aggregation = "average"
        sort = raw.get("sort")
        if sort not in {"asc", "desc"}:
            sort = None
        return_all = bool(raw.get("return_all", False))
        try:
            limit = int(raw.get("limit", 5))
        except (TypeError, ValueError):
            limit = 5
        limit = None if return_all else max(1, min(limit, 50))
        term = str(raw.get("term") or "").strip() or None
        include_guide = bool(raw.get("include_guide", False))
        clarification_needed = bool(raw.get("clarification_needed", False))
        clarification_question = str(raw.get("clarification_question") or "").strip() or None

        if re.search(r"내일\s+모레", question):
            filters["dates"] = [
                _fmt_ymd(self.bot.base_date + timedelta(days=1)),
                _fmt_ymd(self.bot.base_date + timedelta(days=2)),
            ]
        if any(phrase in question for phrase in ["전부", "모두", "전체", "다 알려", "다 보여"]):
            return_all = True
            limit = None
        if operation == "distinct" and len(filters["dates"]) > 1 and "예측일자" not in select:
            select.insert(0, "예측일자")

        index_aliases = ["예보지수", "바다낚시지수", "낚시지수", "지수 등급"]
        asks_index = any(alias in question for alias in index_aliases) or bool(
            re.search(r"지수(?:와|의)?\s*(?:알려|보여|의미|뜻|설명)", question)
            and not filters["forecast_indexes"]
        )
        asks_definition = bool(re.search(r"(?:뭐야|무엇|뜻|의미|정의|설명|어떤\s*(?:뜻|단계|상태)|어떻게\s*(?:계산|산출))", question))
        ranking_words = ["가장", "제일", "최고", "최저", "순위", "랭킹", "비교", "더 좋"]
        weather_request = bool(re.search(r"날씨|기상", question))
        if weather_request:
            mentioned_regions = self.bot._parse_regions(question) or []
            if mentioned_regions:
                filters["regions"] = mentioned_regions
            mentioned_dates = dates_in(question, self.bot.base_date)
            if mentioned_dates:
                filters["dates"] = mentioned_dates
        has_lookup_filter = bool(filters["dates"] or filters["regions"] or filters["fish"] or filters["times"])
        level_term = next((level for level in ["매우좋음", "매우나쁨", "좋음", "보통", "나쁨"] if level in question), None)
        fish_term = next((fish for fish in self.bot.fish if fish in question), None)
        asks_fish_info = bool(fish_term and re.search(r"(?:특징|서식|사는\s*곳|어떤\s*어종|활동\s*수온)", question))
        # Do not silently drop an explicitly requested, unsupported species.
        # Without this guard, a question such as "호랑이 잡기 좋은 곳" can be
        # misread as a generic good-fishing recommendation and return the
        # previous/default recommendation set.
        unknown_species = None
        # "호랑이 잡기" has an explicit target, while phrases such as
        # "해양 낚시", "그곳에서 낚시", and "어디에 낚시" do not.  Treating
        # every word before "낚시" as a species caused ordinary fishing
        # questions to be rejected as unknown-fish requests.
        species_match = re.search(r"([가-힣]{2,12})(?:을|를|이|가)?\s*(?:잡기|낚기)", question)
        if species_match:
            candidate = re.sub(r"(?:에서|으로|로|에게|한테|의|에)$", "", species_match.group(1))
            generic_words = {
                "생활", "바다", "민물", "초보", "원투", "찌", "루어", "낚시",
                "오늘", "내일", "모레", "글피", "어제", "이번주", "이번", "에서", "으로", "로",
            }
            known_place_words = set(allowed_values["areas"]) | {"동해", "서해", "황해", "남해"}
            if (candidate not in generic_words and candidate not in known_place_words
                    and not any(fish in candidate or candidate in fish for fish in self.bot.fish)):
                unknown_species = candidate
                operation = "unsupported"
                clarification_needed = False
                clarification_question = None
                raw_reason = f"요청하신 어종 '{candidate}'은(는) 현재 제공되는 어종 목록에 없습니다."
                if not str(raw.get("unsupported_reason") or "").strip():
                    raw["unsupported_reason"] = raw_reason
        environment_terms = []
        for name in ENVIRONMENT_METRIC_GROUPS:
            if name in question and f"{name}점수" not in question:
                canonical_name = "풍속" if name == "바람" else name
                if canonical_name not in environment_terms:
                    environment_terms.append(canonical_name)
        # A generic weather question means the environmental values available in this CSV.
        # Keep its interpretation stable even when the LLM labels it unsupported.
        if weather_request and not asks_definition and not any(word in question for word in ranking_words):
            environment_terms = ["기온", "수온", "풍속", "파고", "유속"]
            operation = "detail"
        environment_lookup = bool(
            operation == "detail"
            and environment_terms
            and (has_lookup_filter or weather_request)
            and not asks_definition
            and not any(word in question for word in ranking_words)
        )

        if level_term and asks_definition:
            operation = "explain"
            term = level_term
            include_guide = True
            clarification_needed = False
            clarification_question = None
        elif asks_fish_info:
            operation = "explain"
            term = fish_term
            include_guide = True
            clarification_needed = False
            clarification_question = None
        elif asks_index and has_lookup_filter and not any(word in question for word in ranking_words):
            operation = "detail"
            select = ["예측일자", "지역", "시간", "어종", "총점수", "예보지수"]
            group_by = None
            metric = None
            sort = None
            return_all = True
            limit = None
            include_guide = include_guide or asks_definition
            clarification_needed = False
            clarification_question = None
        elif asks_index and asks_definition:
            operation = "explain"
            term = "예보지수"
            include_guide = True
            clarification_needed = False
            clarification_question = None

        if environment_lookup:
            # Explicit place names in a direct lookup take precedence over model guesses.
            # Leave exclusions/expanded scopes to the planner rather than reversing their meaning.
            if not re.search(r"제외|빼고|말고|주변|근처|인근|전체|모든|전국", question):
                mentioned = self.bot._parse_regions(question) or []
                if mentioned:
                    filters["regions"] = sorted(mentioned)
            environment_fields = []
            for name in environment_terms:
                for field in ENVIRONMENT_METRIC_GROUPS[name]:
                    if field not in environment_fields:
                        environment_fields.append(field)
            # 환경값은 어종별 지수 행에 반복되므로 조회 결과에서 어종 차원을 제거합니다.
            filters["fish"] = []
            filters["fish_types"] = []
            select = ["예측일자", "지역", "시간", *environment_fields]
            group_by = None
            metric = None
            sort = None
            return_all = True
            limit = None
            clarification_needed = False
            clarification_question = None

        recommendation_levels = [] if unknown_species else good_fishing_levels(question)
        recommendation_phrase = re.sub(r"\s+", "", question)
        unrelated_recommendation = (
            not recommendation_levels and not unknown_species
            and re.search(r"(?:좋은|괜찮은)(?:지역|곳|해역|지점)", recommendation_phrase)
            and not re.search(r"낚시|어종|조과|출조|잡기|잡을|바다낚시|지수", question)
        )
        if unrelated_recommendation:
            operation = "unsupported"
        if recommendation_levels:
            # Recover this business constraint even if the model returns an unfiltered detail plan.
            filters["forecast_indexes"] = recommendation_levels
            operation = "detail"
            select = ["예측일자", "지역", "시간", "어종", "총점수", "예보지수"]
            group_by, metric, sort = None, "총점수", "desc"
            environment_lookup = False
            clarification_needed = False
            clarification_question = None
            return_all = any(word in question for word in ["전부", "모두", "전체", "다 알려", "다 보여"])
            count_match = re.search(r"(\d+)\s*(?:개|곳|건)", question)
            limit = None if return_all else (min(50, max(1, int(count_match.group(1)))) if count_match else 5)

        numeric_conditions = []
        has_explicit_threshold = bool(re.search(r"\d+(?:\.\d+)?\s*(?:m/s|미터|m|도|℃)?\s*(?:이하|미만|이상|초과|이내)", question))
        conditions = raw.get("numeric_conditions") if has_explicit_threshold and isinstance(raw.get("numeric_conditions"), list) else []
        for condition in conditions:
            if not isinstance(condition, dict):
                continue
            field = condition.get("field")
            operator = condition.get("operator")
            try:
                value = float(condition.get("value"))
            except (TypeError, ValueError):
                continue
            if field in QUERY_METRICS - {"예보지수"} and operator in {"<", "<=", ">", ">=", "=="}:
                numeric_conditions.append({"field": field, "operator": operator, "value": value})

        if group_by and metric and sort and operation == "aggregate":
            operation = "ranking"
        if any(word in question for word in ["가장", "제일", "최고", "최저"]) and not re.search(r"\d+\s*(?:개|곳)", question):
            limit = 1

        unsupported_reason = str(raw.get("unsupported_reason") or "").strip() or None
        if unrelated_recommendation:
            unsupported_reason = "낚시 관련 지역 추천으로 이해할 수 없는 질문입니다. 낚시할 지역이나 어종을 기준으로 질문해 주세요."
        if unknown_species:
            unsupported_reason = f"요청하신 어종 '{unknown_species}'은(는) 현재 제공되는 어종 목록에 없습니다. 제공되는 어종 기준으로 다시 질문해 주세요."
        if operation == "unsupported":
            reasons = []
            if any(word in question for word in ["실제로", "어획량", "많이 잡", "잡힌 기록"]):
                reasons.append("현재 CSV에는 실제 어획량이나 실제 조황 기록이 없습니다.")
            if any(word in question for word in ["맛집", "식당", "교통", "거리", "숙박"]):
                reasons.append("현재 CSV에는 맛집, 교통, 거리 또는 숙박 정보가 없습니다.")
            if reasons:
                unsupported_reason = " ".join(reasons)

        # Never turn an incomplete direct environmental query into a nationwide query.
        # Rankings and explicit all-region requests legitimately have no named place.
        direct_environment = (operation == "detail" and environment_terms and not asks_definition
                              and not any(word in question for word in ranking_words))
        if direct_environment and not any(word in question for word in ["전체", "전국", "모든 지역", "모든지역"]):
            if not filters["regions"] and not filters["areas"]:
                clarification_needed = True
                clarification_question = "어느 지역의 예보가 궁금하신가요? 서비스 지역 이름을 확인해 주세요."
            elif not filters["dates"]:
                clarification_needed = True
                clarification_question = "언제의 예보가 궁금하신가요? 오늘, 내일처럼 날짜를 알려주세요."

        plan = {
            "operation": operation,
            "filters": filters,
            "numeric_conditions": numeric_conditions,
            "select": select,
            "group_by": group_by,
            "metric": metric,
            "aggregation": aggregation,
            "sort": sort,
            "limit": limit,
            "return_all": return_all,
            "include_guide": include_guide,
            "environment_lookup": environment_lookup,
            "good_fishing_recommendation": bool(recommendation_levels),
            "term": term,
            "clarification_needed": clarification_needed,
            "clarification_question": clarification_question,
            "unsupported_reason": unsupported_reason,
        }
        return refine_plan(plan, question, self.bot)


class GenericCSVQueryEngine:
    FILTER_MAP = {
        "dates": "예측일자",
        "times": "시간",
        "areas": "권역",
        "regions": "지역",
        "fish": "어종",
        "fish_types": "어종타입",
        "tides": "물때",
        "forecast_indexes": "예보지수",
    }

    def __init__(self, bot: OceanChatbot, guide: Optional[OfficialGuide] = None):
        self.bot = bot
        self.guide = guide

    @staticmethod
    def _matches(row_value: str, requested: Sequence[str], partial: bool = False) -> bool:
        if not requested:
            return True
        if partial:
            return any(value in row_value or row_value in value for value in requested)
        return row_value in requested

    @staticmethod
    def _condition_matches(value: Optional[float], operator: str, target: float) -> bool:
        if value is None:
            return False
        return {
            "<": value < target,
            "<=": value <= target,
            ">": value > target,
            ">=": value >= target,
            "==": value == target,
        }[operator]

    def filter_rows(self, plan: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows = list(self.bot.rows)
        for key, column in self.FILTER_MAP.items():
            requested = plan["filters"].get(key, [])
            if not requested:
                continue
            values = [value.replace("-", "") for value in requested] if key == "dates" else requested
            partial = key in {"areas", "regions", "fish", "fish_types"}
            # Registered names are identities: 포항 must not also match 방포항.
            known_values = {str(row.get(column, "")) for row in self.bot.rows}
            rows = [row for row in rows if any(
                str(row.get(column, "")) == value if value in known_values
                else self._matches(str(row.get(column, "")), [value], partial)
                for value in values)]
        for key, values in plan.get("exclude_filters", {}).items():
            column = self.FILTER_MAP[key]
            rows = [row for row in rows if row.get(column) not in values]
        for condition in plan["numeric_conditions"]:
            field = condition["field"]
            rows = [
                row for row in rows
                if self._condition_matches(row.get(f"__{field}_num"), condition["operator"], condition["value"])
            ]
        return rows

    @staticmethod
    def _display_value(column: str, value: Any) -> Any:
        if column in {"예측일자", "생산일"}:
            return OceanChatbot._pretty_ymd(str(value))
        return value

    @staticmethod
    def _metric_value(row: Dict[str, Any], metric: str) -> Optional[float]:
        if metric == "예보지수":
            value = INDEX_ORDER.get(row.get("예보지수"))
            return float(value) if value is not None else None
        value = row.get(f"__{metric}_num")
        return float(value) if isinstance(value, (int, float)) else None

    @staticmethod
    def _aggregate(values: Sequence[float], method: str) -> float:
        if method == "min":
            return min(values)
        if method == "max":
            return max(values)
        if method == "sum":
            return sum(values)
        if method == "count":
            return float(len(values))
        return mean(values)

    def _distinct(self, rows: Sequence[Dict[str, Any]], plan: Dict[str, Any]) -> Dict[str, Any]:
        fields = plan["select"] or ([plan["group_by"]] if plan["group_by"] else ["지역"])
        fields = [field for field in fields if field]
        seen = set()
        items = []
        for row in rows:
            key = tuple(str(row.get(field, "")) for field in fields)
            if key in seen:
                continue
            seen.add(key)
            items.append({field: self._display_value(field, row.get(field)) for field in fields})
        items.sort(key=lambda item: tuple(str(item.get(field, "")) for field in fields))
        total = len(items)
        if plan["limit"] is not None:
            items = items[: plan["limit"]]
        else:
            items = items[:200]
        requested_dates = plan["filters"].get("dates", [])
        dates_with_results = sorted({OceanChatbot._pretty_ymd(row["예측일자"]) for row in rows}) if requested_dates else []
        return {
            "kind": "distinct",
            "fields": fields,
            "total": total,
            "items": items,
            "truncated": total > len(items),
            "display_limit_reached": plan["limit"] is None and total > len(items),
            "requested_dates": requested_dates,
            "dates_with_results": dates_with_results,
            "dates_without_results": [value for value in requested_dates if value not in dates_with_results],
        }

    def _detail(self, rows: Sequence[Dict[str, Any]], plan: Dict[str, Any]) -> Dict[str, Any]:
        fields = plan["select"] or DETAIL_COLUMNS
        items = []
        seen = set()
        for row in rows:
            item = {field: self._display_value(field, row.get(field)) for field in fields if row.get(field) not in (None, "")}
            item_key = tuple((field, str(item.get(field, ""))) for field in fields)
            if item_key in seen:
                continue
            seen.add(item_key)
            items.append(item)
        items.sort(key=lambda item: (str(item.get("예측일자", "")), str(item.get("지역", "")), str(item.get("시간", ""))))
        if plan.get("good_fishing_recommendation"):
            items.sort(key=lambda item: _to_float(item.get("총점수")) or 0, reverse=True)
        if plan.get("unique_regions"):
            seen_regions = set()
            unique_items = []
            for item in items:
                if item.get("지역") not in seen_regions:
                    seen_regions.add(item.get("지역"))
                    unique_items.append(item)
            items = unique_items
        total = len(items)
        limit = plan["limit"] if plan["limit"] is not None else 200
        result = {"kind": "detail", "total": total, "items": items[:limit], "truncated": total > limit,
                  "display_limit_reached": plan["limit"] is None and total > limit}
        if plan.get("unique_regions"):
            result["unique_regions"] = True
            result["requested_count"] = plan["limit"]
        if plan.get("good_fishing_recommendation"):
            result["good_fishing_recommendation"] = True
            result["forecast_indexes"] = plan["filters"]["forecast_indexes"]
        if plan.get("environment_lookup"):
            identity_fields = [field for field in ["예측일자", "지역", "시간"] if field in fields]
            environment_fields = [field for field in fields if field in QUERY_METRICS]
            conflicts = []
            grouped_values: Dict[Tuple[str, ...], Dict[str, set]] = defaultdict(lambda: defaultdict(set))
            for row in rows:
                identity = tuple(str(self._display_value(field, row.get(field))) for field in identity_fields)
                for field in environment_fields:
                    value = row.get(field)
                    if value not in (None, ""):
                        grouped_values[identity][field].add(str(value))
            for identity, values_by_field in grouped_values.items():
                for field, values in values_by_field.items():
                    if len(values) > 1:
                        location = ", ".join(f"{name}: {value}" for name, value in zip(identity_fields, identity))
                        conflicts.append(f"{location}의 {field} 값이 여러 개입니다: {', '.join(sorted(values))}")
            if conflicts:
                result["data_warnings"] = conflicts
        return result

    def _hazard_ranked(self, rows: Sequence[Dict[str, Any]], plan: Dict[str, Any]) -> Dict[str, Any]:
        """Rank date/region/time by relative wind or wave values, deduping fish rows."""
        grouped: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            key = (str(row.get("예측일자", "")), str(row.get("지역", "")), str(row.get("시간", "")))
            grouped[key].append(row)

        candidates = []
        for (forecast_date, region, time_slot), group_rows in grouped.items():
            item: Dict[str, Any] = {"예측일자": OceanChatbot._pretty_ymd(forecast_date),
                                    "지역": region, "시간": time_slot}
            for field in ("최소풍속", "평균풍속", "최대풍속", "최소파고", "평균파고", "최대파고"):
                values = [self._metric_value(row, field) for row in group_rows]
                values = [value for value in values if value is not None]
                if values:
                    # If duplicate fish rows disagree, retain the wider range and higher mean.
                    item[field] = min(values) if field.startswith("최소") else max(values)
            if "평균풍속" in item or "평균파고" in item:
                candidates.append(item)

        if not candidates:
            return {"kind": "no_match", "matched_rows": 0, "hazard_lookup": True}

        wind_values = [item.get("평균풍속") for item in candidates if item.get("평균풍속") is not None]
        wave_values = [item.get("평균파고") for item in candidates if item.get("평균파고") is not None]

        def percentile(value: Optional[float], values: List[float]) -> float:
            if value is None or not values:
                return -1.0
            return sum(candidate <= value for candidate in values) / len(values)

        for item in candidates:
            wind_rank = percentile(item.get("평균풍속"), wind_values)
            wave_rank = percentile(item.get("평균파고"), wave_values)
            item["상대순위"] = max(wind_rank, wave_rank)
            reasons = []
            if wind_rank >= 0 and wind_rank == item["상대순위"]:
                reasons.append("풍속")
            if wave_rank >= 0 and wave_rank == item["상대순위"]:
                reasons.append("파고")
            item["높은항목"] = reasons

        candidates.sort(key=lambda item: (-item["상대순위"], -max(item.get("평균풍속", 0), 0),
                                          -max(item.get("평균파고", 0), 0), item["지역"], item["시간"]))
        limit = plan.get("limit") or 5
        items = candidates[:limit]
        return {"kind": "hazard_ranking", "hazard_lookup": True, "total": len(candidates),
                "items": items, "truncated": len(candidates) > len(items),
                "display_limit_reached": False}

    @staticmethod
    def _fishing_outlook(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        grouped: Dict[Tuple[str, str, str], set] = defaultdict(set)
        fish_counts: Dict[Tuple[str, str, str], set] = defaultdict(set)
        for row in rows:
            key = (str(row.get("예측일자", "")), str(row.get("지역", "")), str(row.get("시간", "")))
            level = row.get("예보지수")
            if level in INDEX_ORDER:
                grouped[key].add(level)
                if row.get("어종"):
                    fish_counts[key].add(row["어종"])
        items = []
        for (forecast_date, region, time_slot), levels in grouped.items():
            items.append({"예측일자": OceanChatbot._pretty_ymd(forecast_date), "지역": region,
                          "시간": time_slot,
                          "지수목록": sorted(levels, key=lambda level: INDEX_ORDER[level]),
                          "대상어종수": len(fish_counts[(forecast_date, region, time_slot)])})
        items.sort(key=lambda item: (item["예측일자"], item["지역"], item["시간"]))
        return {"kind": "fishing_outlook", "fishing_outlook_lookup": True,
                "total": len(items), "items": items}

    def _grouped(self, rows: Sequence[Dict[str, Any]], plan: Dict[str, Any]) -> Dict[str, Any]:
        operation = plan["operation"]
        group_by = plan["group_by"]
        if operation == "trend" and not group_by:
            group_by = "예측일자"
        if not group_by:
            group_by = "지역"
        metric = plan["metric"] or "총점수"
        method = plan["aggregation"]
        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[str(row.get(group_by, ""))].append(row)
        items = []
        for group, group_rows in groups.items():
            values = [self._metric_value(row, metric) for row in group_rows]
            values = [value for value in values if value is not None]
            if not values:
                continue
            value = self._aggregate(values, method)
            representative = max(group_rows, key=lambda row: row.get("__총점수_num") or -1)
            item: Dict[str, Any] = {
                "group": self._display_value(group_by, group),
                "value": round(value, 2),
                "metric": metric,
                "aggregation": method,
                "count": len(values),
            }
            for field in ("예측일자", "시간", "어종"):
                if representative.get(field):
                    item[f"대표{field}"] = self._display_value(field, representative[field])
            if representative.get("예보지수"):
                item["대표예보지수"] = representative["예보지수"]
            if representative.get("총점수"):
                item["대표총점수"] = representative["총점수"]
            items.append(item)
        reverse = plan["sort"] != "asc"
        items.sort(key=lambda item: item["value"], reverse=reverse)
        total = len(items)
        limit = plan["limit"] if plan["limit"] is not None else 200
        if plan.get("all_ties") and items:
            limit = sum(item["value"] == items[0]["value"] for item in items)
        return {
            "kind": operation,
            "group_by": group_by,
            "metric": metric,
            "aggregation": method,
            "sort": "desc" if reverse else "asc",
            "total": total,
            "items": items[:limit],
            "truncated": total > limit,
            "display_limit_reached": plan["limit"] is None and total > limit,
        }

    def _count(self, rows: Sequence[Dict[str, Any]], plan: Dict[str, Any]) -> Dict[str, Any]:
        fields = plan["select"] or ([plan["group_by"]] if plan["group_by"] else [])
        fields = [field for field in fields if field]
        if fields:
            unique = {tuple(str(row.get(field, "")) for field in fields) for row in rows}
            return {"kind": "count", "count": len(unique), "counted_fields": fields, "matched_rows": len(rows)}
        return {"kind": "count", "count": len(rows), "counted_fields": [], "matched_rows": len(rows)}

    def _aggregate_result(self, rows: Sequence[Dict[str, Any]], plan: Dict[str, Any]) -> Dict[str, Any]:
        metric = plan["metric"] or "총점수"
        values = [self._metric_value(row, metric) for row in rows]
        values = [value for value in values if value is not None]
        if not values:
            return {"kind": "aggregate", "metric": metric, "value": None, "count": 0}
        return {
            "kind": "aggregate",
            "metric": metric,
            "aggregation": plan["aggregation"],
            "value": round(self._aggregate(values, plan["aggregation"]), 2),
            "count": len(values),
        }

    def execute(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        if plan["operation"] == "unsupported":
            return {"kind": "unsupported", "reason": plan["unsupported_reason"] or "현재 지수 파일로 처리할 수 없는 질문입니다."}
        if plan["clarification_needed"]:
            return {"kind": "clarification", "question": plan["clarification_question"] or "어떤 기준으로 조회할지 알려주세요."}
        if plan["operation"] == "explain":
            term = plan["term"] or "예보지수"
            if self.guide:
                guide_result = self.guide.lookup(term)
                if guide_result:
                    return {"kind": "explain", **guide_result}
            definition = TERM_GLOSSARY.get(term)
            if definition is None:
                for key, value in TERM_GLOSSARY.items():
                    if key in term or term in key:
                        definition = value
                        term = key
                        break
            return {"kind": "explain", "term": term, "definition": definition, "available_terms": sorted(TERM_GLOSSARY)}

        rows = self.filter_rows(plan)
        if not rows:
            return {"kind": "no_match", "matched_rows": 0, "good_fishing_recommendation": plan.get("good_fishing_recommendation", False), "forecast_indexes": plan["filters"].get("forecast_indexes", []), "hazard_lookup": plan.get("hazard_lookup", False)}
        if plan.get("hazard_lookup"):
            return self._hazard_ranked(rows, plan)
        if plan.get("fishing_outlook_lookup"):
            return self._fishing_outlook(rows)
        if plan["operation"] == "distinct":
            return self._distinct(rows, plan)
        if plan["operation"] == "detail":
            result = self._detail(rows, plan)
            if plan.get("include_guide") and self.guide:
                result["guide"] = self.guide.context_for_result(plan, result)
            return result
        if plan["operation"] == "count":
            return self._count(rows, plan)
        if plan["operation"] == "aggregate" and not plan["group_by"]:
            return self._aggregate_result(rows, plan)
        return self._grouped(rows, plan)


class HybridOceanChatbot:
    def __init__(self, data_file: Path):
        self.legacy = OceanChatbot(data_file)
        self.base_date = self.legacy.base_date
        self.guide = OfficialGuide(GUIDE_FILE)
        self.client = OllamaClient(OLLAMA_URL, OLLAMA_MODEL, LLM_TIMEOUT)
        self.planner = LLMQueryPlanner(self.client, self.legacy)
        self.query_engine = GenericCSVQueryEngine(self.legacy, self.guide)

    def meta(self) -> Dict[str, Any]:
        meta = self.legacy.meta()
        meta["llm"] = self.client.status()
        meta["official_guide"] = self.guide.meta()
        meta["architecture"] = "LLM JSON → Python CSV → LLM answer"
        return meta

    @staticmethod
    def _fallback_result_text(result: Dict[str, Any]) -> str:
        kind = result.get("kind")
        if kind == "clarification":
            return str(result.get("question"))
        if kind == "unsupported":
            return f"현재 지수 파일을 기준으로는 답변할 수 없습니다. {result.get('reason', '')}".strip()
        if kind == "no_match" and result.get("good_fishing_recommendation"):
            levels = "·".join(result.get("forecast_indexes", []))
            return f"요청한 날짜·지역·시간·어종 조건에서 예보지수 {levels}에 해당하는 자료가 없습니다. 다른 등급을 대신 추천하지 않습니다."
        if kind == "no_match" and result.get("hazard_lookup"):
            return "해당 날짜와 지역에 비교할 풍속·파고 자료가 없습니다. 날짜나 해역을 바꿔 다시 물어보세요."
        if kind == "no_match" and result.get("fishing_outlook_lookup"):
            return "해당 날짜와 지역의 바다낚시지수 자료를 찾지 못했습니다. 날짜나 지역을 확인해 주세요."
        if kind == "no_match":
            return "요청한 조건에 맞는 데이터가 현재 지수 파일에 없습니다."
        if kind == "hazard_ranking":
            items = result.get("items", [])
            if not items:
                return "해당 날짜에 비교할 풍속·파고 자료가 없습니다."
            dates = {item.get("예측일자") for item in items if item.get("예측일자")}
            date_heading = f"조회 날짜: {next(iter(dates))}\n" if len(dates) == 1 else ""
            lines = ["평균풍속 또는 평균파고가 조회 대상 중 상대적으로 높은 지역과 시간대입니다."]
            for index, item in enumerate(items, 1):
                time_label = {"AM": "오전", "PM": "오후", "DY": "종일"}.get(item.get("시간"), item.get("시간", ""))
                details = []
                if item.get("평균풍속") is not None:
                    wind = f"평균풍속 {item['평균풍속']}m/s"
                    if item.get("최소풍속") is not None and item.get("최대풍속") is not None:
                        wind += f" (예상 범위 {item['최소풍속']}~{item['최대풍속']}m/s)"
                    details.append(wind)
                if item.get("평균파고") is not None:
                    wave = f"평균파고 {item['평균파고']}m"
                    if item.get("최소파고") is not None and item.get("최대파고") is not None:
                        wave += f" (예상 범위 {item['최소파고']}~{item['최대파고']}m)"
                    details.append(wave)
                lines.append(f"{index}. {item.get('지역')} {time_label} — " + ", ".join(details))
            lines.append("상대 비교 결과이며 안전 여부를 판정한 것은 아닙니다. 출조 전 최신 해상특보와 현장 상황을 확인해 주세요.")
            return date_heading + "\n".join(lines)
        if kind == "fishing_outlook":
            items = result.get("items", [])
            if not items:
                return "해당 날짜와 지역의 바다낚시지수 자료를 찾지 못했습니다. 날짜나 지역을 확인해 주세요."
            dates = {item.get("예측일자") for item in items if item.get("예측일자")}
            shared_date = next(iter(dates)) if len(dates) == 1 else None
            date_heading = f"조회 날짜: {shared_date}\n" if shared_date else ""
            grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
            for item in items:
                grouped[(item.get("예측일자", ""), item.get("지역", ""))].append(item)
            lines = []
            all_levels = set()
            for (forecast_date, region), periods in grouped.items():
                descriptions = []
                for period in periods:
                    levels = period.get("지수목록", [])
                    all_levels.update(levels)
                    level_text = "·".join(levels)
                    if len(levels) > 1:
                        level_text = f"어종에 따라 {level_text}으로 차이가 있습니다"
                    time_label = {"AM": "오전", "PM": "오후", "DY": "종일"}.get(period.get("시간"), period.get("시간", ""))
                    descriptions.append(f"{time_label}은 {level_text}")
                if len(periods) > 1 and all(len(p.get("지수목록", [])) == 1 for p in periods):
                    levels = {p["지수목록"][0] for p in periods}
                    if len(levels) == 1:
                        period_names = "·".join({"AM": "오전", "PM": "오후", "DY": "종일"}.get(p.get("시간"), p.get("시간", "")) for p in periods)
                        summary = f"{period_names} 모두 {next(iter(levels))}입니다"
                    else:
                        summary = ", ".join(descriptions) + "입니다"
                else:
                    summary = ", ".join(descriptions) + "입니다"
                prefix = f"{forecast_date} " if not shared_date else ""
                lines.append(f"{prefix}{region}의 바다낚시지수는 {summary}.")
            if all_levels and max(INDEX_ORDER[level] for level in all_levels) <= INDEX_ORDER["나쁨"]:
                lines.append("지수가 좋지 않은 편입니다. 원하는 어종이 있으면 그 어종 기준으로 다시 확인해 드릴게요.")
            elif "보통" in all_levels and not (all_levels & {"좋음", "매우좋음"}):
                lines.append("지수는 낚시 여건을 나타내는 참고 정보이며 실제 조과를 보장하지 않습니다.")
            return date_heading + "\n".join(lines)
        if kind == "explain":
            return str(result.get("definition") or f"'{result.get('term')}'에 대한 공식 정의가 현재 자료에 없습니다.")
        if kind == "count":
            return f"조건에 맞는 결과는 {result.get('count', 0)}개입니다."
        if kind == "aggregate" and result.get("metric") == "총점수":
            return "점수는 내부 추천 기준으로 사용합니다. 지역별 지수와 날짜·시간·어종을 확인해 주세요."
        if kind == "aggregate":
            return f"{result.get('metric')} {result.get('aggregation')} 결과는 {result.get('value')}입니다."
        items = result.get("items", [])
        if not items:
            return "조건에 맞는 결과를 찾지 못했습니다."
        # Show a shared date once without changing the underlying query rows.
        date_keys = ("예측일자", "대표예측일자")
        dates = [next((item[key] for key in date_keys if item.get(key)), None) for item in items]
        shared_date = dates[0] if dates and dates[0] and all(date == dates[0] for date in dates) else None
        if shared_date:
            items = [{key: value for key, value in item.items() if key not in date_keys} for item in items]
        date_heading = f"조회 날짜: {shared_date}\n" if shared_date else ""
        if kind == "distinct":
            fields = [field for field in result.get("fields", []) if not (shared_date and field in date_keys)]
            lines = [f"조건에 맞는 중복 없는 결과는 총 {result.get('total', len(items))}개입니다."]
            if result.get("dates_without_results"):
                lines.append(f"결과 없음: {', '.join(result['dates_without_results'])}")
            for item in items:
                lines.append("- " + " / ".join(f"{field}: {item.get(field)}" for field in fields if "총점" not in field))
            if result.get("display_limit_reached"):
                lines.append("표시 한도 때문에 일부 결과는 생략했습니다.")
            return date_heading + "\n".join(lines)
        if result.get("good_fishing_recommendation"):
            levels = "·".join(result.get("forecast_indexes", []))
            lines = [f"예보지수 {levels}만 조회했습니다. 조건에 맞는 지역·시간·어종 조합은 총 {result.get('total', len(items))}건이며, 총점수 높은 순으로 {len(items)}건을 표시합니다.",
                     "같은 지역도 시간대와 어종에 따라 등급이 다릅니다. 지수는 실제 조과를 보장하지 않습니다."]
            if result.get("unique_regions"):
                lines[0] = f"지수 {levels}에 해당하는 {len(items)}개 지역을 추천합니다. 지역마다 대표 시간대·어종 하나를 표시합니다."
                if len(items) < (result.get("requested_count") or 0):
                    lines.append("요청하신 수보다 조건에 맞는 지역이 적어, 해당 지역만 안내합니다.")
        elif kind == "ranking" and len(items) == 1:
            direction = "낮은" if result.get("sort") == "asc" else "높은"
            lines = [f"{result.get('metric', '지수')}이 가장 {direction} 결과입니다."]
        else:
            lines = [f"조건에 맞는 결과는 총 {result.get('total', len(items))}개입니다."]
        for index, item in enumerate(items, 1):
            if "group" in item:
                if item.get("metric") == "총점수":
                    fields = [f"{index}. {item['group']}"]
                    for key in ("대표예측일자", "대표시간", "대표어종", "대표예보지수"):
                        if item.get(key):
                            fields.append(f"{key.removeprefix('대표')}: {item[key]}")
                    lines.append(" · ".join(fields))
                else:
                    metric = item.get("metric")
                    metric_type = next((name for name, fields in ENVIRONMENT_METRIC_GROUPS.items() if metric in fields), None)
                    unit = ENVIRONMENT_UNITS.get("풍속" if metric_type == "바람" else metric_type, "")
                    lines.append(f"{index}. {item['group']} - {metric} {item.get('value')}{unit}")
            else:
                formatted = []
                for key, value in item.items():
                    if "총점" in key:
                        continue
                    metric_type = next((name for name, fields in ENVIRONMENT_METRIC_GROUPS.items() if key in fields), None)
                    unit = ENVIRONMENT_UNITS.get("풍속" if metric_type == "바람" else metric_type, "")
                    display_value = f"{value}{unit}" if unit else value
                    formatted.append(f"{key}: {display_value}")
                lines.append(f"{index}. " + ", ".join(formatted))
        if result.get("display_limit_reached"):
            lines.append("표시 한도 때문에 일부 결과는 생략했습니다.")
        for warning in result.get("data_warnings", []):
            lines.append(f"데이터 확인 필요: {warning}")
        guide = result.get("guide")
        if guide:
            lines.append("")
            lines.append("공식 설명자료 참고:")
            for item in guide.get("index_levels", []):
                lines.append(f"- {item['term']}: {item['definition']}")
            for item in guide.get("fish", []):
                lines.append(f"- {item['term']} 특징: {item['definition']}")
            if guide.get("notice"):
                lines.append(f"- 참고: {guide['notice']}")
        return date_heading + "\n".join(lines)

    @staticmethod
    def _environment_answer_is_grounded(reply: str, result: Dict[str, Any]) -> bool:
        """Reject environment prose that drops or invents numeric CSV values."""
        if re.search(r"(?:^|\n)\s*[-*]\s|[*_#`]", reply):
            return False
        if any(phrase in reply for phrase in ["낮은 수준", "높은 수준", "쾌적", "따뜻한 편", "추운 편"]):
            return False
        expected_numbers = set()
        for item in result.get("items", []):
            for field, value in item.items():
                if field not in QUERY_METRICS:
                    continue
                value_text = str(value)
                expected_numbers.add(value_text)
                if not re.search(rf"(?<![\d.]){re.escape(value_text)}(?![\d.])", reply):
                    return False

            region = str(item.get("지역", ""))
            forecast_date = str(item.get("예측일자", ""))
            time_slot = str(item.get("시간", ""))
            time_aliases = {
                "AM": ("오전",),
                "PM": ("오후",),
                "DY": ("하루", "종일"),
            }
            if region and region not in reply:
                return False
            if forecast_date and forecast_date not in reply:
                return False
            if time_slot and not any(alias in reply for alias in time_aliases.get(time_slot, (time_slot,))):
                return False

        without_dates = re.sub(r"20\d{2}-\d{2}-\d{2}", "", reply)
        reply_numbers = re.findall(r"(?<![\d.])-?\d+(?:\.\d+)?(?![\d.])", without_dates)
        return all(number in expected_numbers for number in reply_numbers)

    def _environment_result_text(self, result: Dict[str, Any], date_once: bool = False) -> str:
        """Natural, deterministic fallback when the LLM prose fails validation."""
        items = result.get("items", [])
        if not items:
            return "요청한 조건에 맞는 환경 예보가 현재 지수 파일에 없습니다."

        canonical_groups = {
            name: fields for name, fields in ENVIRONMENT_METRIC_GROUPS.items() if name != "바람"
        }
        grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
        for item in items:
            grouped[(str(item.get("예측일자", "")), str(item.get("지역", "")))].append(item)

        paragraphs = []
        time_names = {"AM": "오전", "PM": "오후", "DY": "종일"}
        for (forecast_date, region), group_items in grouped.items():
            group_items.sort(key=lambda item: {"AM": 0, "PM": 1, "DY": 2}.get(str(item.get("시간")), 3))
            lines = []
            for name, fields in canonical_groups.items():
                if not any(field in item for field in fields for item in group_items):
                    continue
                unit = ENVIRONMENT_UNITS[name]
                average_field = next(field for field in fields if field.startswith("평균"))
                averages = []
                ranges = []
                for item in group_items:
                    time_name = time_names.get(str(item.get("시간", "")), str(item.get("시간", "")))
                    average = item.get(average_field)
                    if average not in (None, ""):
                        averages.append(f"{time_name} {average}{unit}")
                    low, high = item.get(fields[0]), item.get(fields[2])
                    if low not in (None, "") and high not in (None, ""):
                        ranges.append(f"{time_name} {low}~{high}{unit}")
                if len(group_items) == 1:
                    item = group_items[0]
                    time_name = time_names.get(str(item.get("시간", "")), str(item.get("시간", "")))
                    average = item.get(average_field)
                    low, high = item.get(fields[0]), item.get(fields[2])
                    label = name
                    if average not in (None, ""):
                        particle = "은" if (ord(label[-1]) - 0xAC00) % 28 else "는"
                        if date_once:
                            sentence = f"평균 {label}{particle} {average}{unit}입니다."
                        else:
                            sentence = f"{forecast_date} {time_name} {region}의 평균 {label}{particle} {average}{unit}입니다."
                        if low not in (None, "") and high not in (None, ""):
                            sentence += f" 예상 {label} 범위는 {low}~{high}{unit}입니다."
                        lines.append(sentence)
                        continue
                if averages:
                    particle = "은" if (ord(name[-1]) - 0xAC00) % 28 else "는"
                    lines.append(f"평균{name}{particle} {', '.join(averages)}입니다.")
                if ranges:
                    lines.append(f"예상 범위는 {', '.join(ranges)}입니다.")
            if date_once:
                date_time = forecast_date
                if len(group_items) == 1:
                    date_time += f" {time_names.get(str(group_items[0].get('시간', '')), '')}"
                heading = f"{date_time} {region}의 환경 예보입니다.\n"
            else:
                heading = "" if len(group_items) == 1 else f"{forecast_date} {region}의 환경 예보입니다.\n"
            paragraphs.append(heading + "\n".join(lines))

        text = "\n\n".join(paragraphs)
        warnings = result.get("data_warnings", [])
        if warnings:
            text += "\n\n" + "\n".join(f"데이터 확인 필요: {warning}" for warning in warnings)
        return text

    def _natural_answer(self, question: str, plan: Dict[str, Any], result: Dict[str, Any]) -> str:
        system = """당신은 해양예보 챗봇의 답변 작성자입니다.
반드시 제공된 Python 계산 결과에 있는 사실만 사용해 한국어로 답하세요.
일반 사용자에게는 예보지수를 "지수", 기타어종을 "기타"로 표현하세요.
총점수·대표총점수·점수 수치는 내부 정렬용이므로 답변에 출력하지 마세요.
지역·날짜·시간·어종·지수 중심으로 안내하세요.
조회 결과에 없는 지역을 추가하지 마세요. 한 시간대의 환경 조회는 날짜·시간·지역을 먼저 밝히고 평균값과 예상 범위를 자연스럽게 이어 쓰세요. "평균풍속은 오후" 같은 어순은 피하세요.
결과에 없는 숫자, 지역, 어종, 원인, 산식을 추측하지 마세요.
이 데이터에는 실제 어획량이나 실제로 잡혔다는 기록이 없습니다.
사용자가 "잡히는 지역"이라고 말해도 반드시 "해당 어종 예보지수를 제공하는 지역"으로 표현하세요.
"잡힌다", "잡혔다", "어획 가능성이 높다"고 단정하지 마세요.
사용자가 실제 어획 여부를 직접 묻지 않았다면 위 제한사항을 답변에 따로 나열하지 마세요.
공식 설명자료의 서식·활동·조과 표현은 반드시 "자료상 경향", "상대적으로 유리할 수 있음"으로 완화하세요.
"조과가 좋다"거나 특정 어종이 반드시 서식·어획된다고 단정하지 마세요.
매우좋음도 실제 어획을 보장하지 않는 지수 기준상 유리한 단계라고 설명하세요.
전체 목록 요청은 항목을 빠뜨리지 말고 날짜와 지역별로 읽기 쉽게 정리하세요.
truncated는 요청한 순위 개수만 선택한 경우에도 참일 수 있습니다. 표시 한도나 결과 생략 안내는 display_limit_reached가 참일 때만 하세요. 가장 낮거나 높은 결과 하나를 요청하면 전체 후보 개수를 나열하지 말고 해당 결과를 바로 안내하세요.
환경 조회는 DB 컬럼명을 나열하지 말고 평균값을 먼저 자연스러운 문장으로 말한 뒤 최소~최대 범위를 덧붙이세요.
환경 조회의 AM은 오전, PM은 오후, DY는 종일로 표현하고, 날짜·지역·시간대와 제공된 모든 측정값을 빠뜨리지 마세요.
답변은 Markdown을 사용해 읽기 쉽게 구성하세요. 결과가 여러 항목이면 제목(## 또는 ###), 글머리표(-), 빈 줄을 적극적으로 사용하세요.
지역별 환경 조회는 지역·날짜·시간대를 제목 또는 첫 줄에 먼저 밝히고, 측정값은 항목별 글머리표로 나누세요.
풍속·파고처럼 반복되는 측정값은 한 줄에 하나씩 쓰고 핵심 수치만 **굵게** 표시하세요. 평균값과 예상 범위는 같은 항목 안에서 짧게 이어 쓰세요.
답변 첫 부분에는 결론이나 가장 중요한 정보를 1~2문장으로 요약하세요. 상세 항목 뒤에는 필요한 경우 빈 줄을 둔 뒤 주의사항을 별도 문단으로 안내하세요.
한 문단은 2~3문장을 넘기지 말고, 괄호 안에 여러 정보를 몰아넣지 마세요. 데이터에 없는 값은 만들지 마세요.
환경값이 낮다·높다·쾌적하다처럼 Python 결과만으로 판단할 수 없는 평가는 덧붙이지 마세요.
Python 결과에 없는 반올림값, 차이값, 행 개수 등 새로운 숫자를 계산하거나 추가하지 마세요.
결과가 없거나 지원하지 않는 질문이면 그 사실과 가능한 대안을 간결하게 안내하세요.
결과의 날짜가 모두 같으면 답변 첫머리에 날짜를 한 번만 표시하고 항목마다 반복하지 마세요. 여러 날짜가 섞이면 각 항목의 날짜를 유지하세요.
JSON이나 내부 처리 과정을 노출하지 말고, 답을 먼저 말한 뒤 필요한 근거를 설명하세요."""
        payload = {
            "question": question,
            "base_date": _fmt_ymd(self.base_date),
            "query_plan": plan,
            "python_result": result,
        }
        return self.client.chat(system, json.dumps(payload, ensure_ascii=False), max_tokens=900)

    @staticmethod
    def _public_text(text: str) -> str:
        text = text.replace("예보지수", "지수").replace("기타어종", "기타")
        text = text.replace("CSV 데이터", "데이터").replace("CSV 자료", "자료")
        text = text.replace("총점수 높은 순으로", "추천순으로")
        text = re.sub(r"(?:대표)?총점수?\s*[:：]?\s*-?\d+(?:\.\d+)?(?:점)?\s*[,，]?\s*", "", text)
        text = re.sub(r"\bAM\b", "오전", text)
        text = re.sub(r"\bPM\b", "오후", text)
        text = re.sub(r"\bDY\b", "종일", text)
        return text.replace("총점수", "추천 기준").replace("예측일자:", "날짜:")

    def answer(self, text: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Keep conversational slots in the requesting browser, never in the shared bot."""
        text = normalize(text)
        previous = context if isinstance(context, dict) else {}
        if re.search(r"그곳|그\s*지역", text) and not (self.legacy._parse_regions(text) or []):
            context_regions = previous.get("regions", [])
            context_regions = context_regions if isinstance(context_regions, list) else []
            last_regions = previous.get("last_regions", [])
            last_regions = last_regions if isinstance(last_regions, list) else []
            candidates = list(dict.fromkeys(
                region for region in [*context_regions, *last_regions]
                if region in self.legacy.regions
            ))
            if len(candidates) != 1:
                reply = "어느 지역을 말씀하시는지 확인이 필요합니다. 지역 이름과 날짜를 알려주세요."
                return {"ok": True, "reply": reply, "engine": "python-clarification",
                        "context": previous, "result": {"kind": "clarification", "question": reply}}
            text = re.sub(r"그곳|그\s*지역", candidates[0], text)
            previous_dates_for_reference = previous.get("dates", [])
            if (isinstance(previous_dates_for_reference, list)
                    and len(previous_dates_for_reference) == 1
                    and not dates_in(text, self.base_date)):
                text = f"{previous_dates_for_reference[0]} {text}"
        early = preflight(text, self.legacy)
        if early:
            return early
        topics = {"수온": "수온", "풍속": "풍속", "바람": "풍속", "파고": "파고",
                  "파도": "파고", "기온": "기온", "유속": "유속", "날씨": "날씨", "기상": "날씨", "바다낚시지수": "바다낚시지수",
                  "낚시 지수": "바다낚시지수", "낚시지수": "바다낚시지수", "지수": "바다낚시지수"}
        previous_regions = previous.get("regions", [])
        previous_regions = [r for r in previous_regions if r in self.legacy.regions] if isinstance(previous_regions, list) else []
        previous_dates = previous.get("dates", [])
        previous_dates = [d for d in previous_dates if isinstance(d, str) and re.fullmatch(r"20\d{2}-\d{2}-\d{2}", d)] if isinstance(previous_dates, list) else []
        previous_times = previous.get("times", [])
        previous_times = [t for t in previous_times if t in ("AM", "PM", "DY")] if isinstance(previous_times, list) else []
        clean = text.strip().strip(' \t\r\n?？!！.。"\'“”‘’')
        correction = bool(previous and re.search(r"말고|바꿔", clean))
        reference = re.search(r"(첫|두|세|네|[1-9])\s*번째\s*지역", clean)
        if reference:
            position = {"첫":1,"두":2,"세":3,"네":4}.get(reference[1], int(reference[1]) if reference[1].isdigit() else 0)
            names = previous.get("last_regions", [])
            if isinstance(names, list) and 0 < position <= len(names) and names[position-1] in self.legacy.regions:
                clean = clean.replace(reference[0], names[position-1])
                correction = True
        if correction:
            clean = re.sub(r"(?:수온|풍속|바람|파고|기온|유속|내일|모레|오늘)\s*말고", "", clean)
            clean = re.sub(r"^아니\s*|(?:으)?로\s*바꿔줘$", "", clean).strip()
        if re.fullmatch(r"(?:다른|새로운|새)\s*질문(?:할게|할게요|할래)?", clean):
            return {"ok": True, "reply": "좋아요. 새로 궁금한 내용을 물어보세요.", "result": {"kind":"conversation"}, "engine":"python-conversation", "context":{}}
        conversation_reply = None
        if re.fullmatch(r"(?:안녕(?:하세요|하십니까)?|반가워(?:요)?|하이)[!~.\s]*", clean):
            conversation_reply = "안녕하세요! 궁금한 지역의 바다낚시지수나 바다 날씨를 물어보세요."
        elif re.fullmatch(r"(?:고마워(?:요)?|감사(?:합니다|해요)?|고맙습니다)[!~.\s]*", clean):
            conversation_reply = "도움이 되셨다니 다행이에요. 더 궁금한 예보가 있으면 말씀해 주세요."
        elif re.fullmatch(r"(?:잘\s*가(?:요)?|안녕히\s*계세요|수고하세요)[!~.\s]*", clean):
            conversation_reply = "다음에 궁금한 바다 날씨나 바다낚시지수가 있으면 찾아주세요!"
        elif re.search(r"싫어|싫지만|귀찮|쉬고\s*싶", clean) and not re.search(r"알려|추천|조회|설명|어디|왜|어떻게", clean):
            conversation_reply = "편하게 쉬어가셔도 좋죠. 다른 날의 바다낚시지수가 궁금하면 말씀해 주세요."
        if conversation_reply:
            return {"ok": True, "reply": conversation_reply, "engine": "python-conversation",
                    "result": {"kind": "conversation"},
                    "context": {"regions": previous_regions, "dates": previous_dates,
                                "times": previous_times, "topic": previous.get("topic"), "pending": False}}
        regions = self.legacy._parse_regions(clean) or []
        topic = next((value for key, value in topics.items() if key in clean), None)
        slots = self.legacy.parse_slots(clean)
        dates = []
        explicit_dates = dates_in(clean, self.base_date)
        if explicit_dates:
            dates = explicit_dates
        elif slots.date_range:
            start, end = slots.date_range
            dates = [_fmt_ymd(start + timedelta(days=i)) for i in range(min((end-start).days + 1, 31))]
        times = [slots.time] if slots.time else []
        # Only carry context into short, recognizable follow-ups. New complex queries
        # (ranking, explanation, exclusions, full lists) retain the normal planner path.
        residue = clean
        for token in sorted([*regions, *topics.keys()], key=len, reverse=True):
            residue = residue.replace(token, "")
        residue = re.sub(r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|\d+\s*일\s*(?:뒤|후|전)(?:에)?|내일모레|내일|모레|오늘|글피|오전|오후|종일", "", residue)
        residue = re.sub(r"알려주세요|알려줘|보여주세요|보여줘|어떻게 돼|어때|궁금해|조회|그럼|그러면|도|은|는|을|를|의|에|와|과|요|[\s?,.!？。]", "", residue)
        simple = not residue and bool(topic or regions or dates or times)
        pending = previous.get("pending") is True and previous.get("topic") in topics.values()
        region_followup = bool(regions and not topic and previous.get("topic") in topics.values() and (previous_dates or previous_times))
        if simple and (topic or pending or region_followup or (previous.get("topic") in topics.values() and (dates or times or correction))):
            topic = topic or previous.get("topic")
            # Switching to another region starts a fresh date selection unless continuing a pending query.
            changed_region = bool(regions and regions != previous_regions and not pending and not correction and not region_followup)
            regions = regions or previous_regions
            dates = dates or ([] if changed_region else previous_dates)
            times = times or ([] if changed_region else previous_times)
            state = {"regions": regions, "dates": dates, "times": times, "topic": topic, "pending": True}
            if not regions or not dates:
                if not regions and not dates:
                    reply = f"어느 지역의 {topic}이 궁금하신가요? 날짜도 함께 알려주세요.\n예: 내일 하조도 {topic} 알려줘"
                elif not regions:
                    reply = f"{', '.join(dates)}의 {topic}을 확인할게요. 어느 지역이 궁금하신가요?"
                else:
                    reply = f"{'·'.join(regions)}의 {topic}을 확인할게요. 언제의 예보가 궁금하신가요? 오늘, 내일처럼 알려주세요."
                return {"ok": True, "reply": reply, "engine": "python-clarification",
                        "context": state, "result": {"kind": "clarification", "question": reply}}
            # Completed direct lookups use validated slots, not another model guess.
            raw = {"operation": "detail", "filters": {"regions": regions, "dates": dates, "times": times}}
            question = f"{' '.join(regions)} {topic} 알려줘"
            plan = self.planner.validate(raw, question)
            result = self.query_engine.execute(plan)
            if plan.get("environment_lookup") and result.get("items"):
                reply = self._environment_result_text(result, date_once=(topic == "날씨"))
                if topic == "날씨":
                    reply = "CSV에 제공되는 기온·수온·풍속·파고·유속 예보입니다.\n" + reply
            else:
                reply = self._fallback_result_text(result)
            state["pending"] = False
            return {"ok": True, "reply": self._public_text(reply), "context": state,
                    "plan": plan, "result": result, "items": result.get("items", []), "engine": "python-context-lookup"}
        payload = self._answer_current(text)
        if clean in self.legacy.regions:
            payload["context"] = {"regions": [clean], "dates": [], "times": [], "pending": False}
        else:
            plan = payload.get("plan", {})
            filters = plan.get("filters", {})
            payload["context"] = {"regions": filters.get("regions", []), "dates": filters.get("dates", []),
                                  "times": filters.get("times", []), "topic": topic, "pending": False}
        payload["context"]["last_regions"] = list(dict.fromkeys(item.get("지역", item.get("group")) for item in payload.get("result", {}).get("items", []) if item.get("지역", item.get("group")) in self.legacy.regions))
        return payload

    def _answer_current(self, text: str) -> Dict[str, Any]:
        # A place name alone is an invitation to ask about that place, not a list query.
        region = text.strip().strip(' \t\r\n?？!！.。"\'“”‘’')
        if region in self.legacy.regions:
            areas = sorted({row["권역"] for row in self.legacy.rows
                            if row["지역"] == region and row.get("권역")})
            location = f"{'·'.join(areas)} 권역에 속한 " if areas else ""
            reply = (f"{region}에 대해 알아볼까요? {region}는 {location}바다낚시지수 제공 지역입니다.\n"
                     "바다낚시지수, 바람, 파도, 수온 중 어떤 정보가 궁금하신가요?\n"
                     f'예를 들어 “내일 {region} 바다낚시지수 알려줘”라고 물어보세요.')
            # Use the appropriate Korean subject particle for the region name.
            particle = "은" if 0xAC00 <= ord(region[-1]) <= 0xD7A3 and (ord(region[-1]) - 0xAC00) % 28 else "는"
            reply = reply.replace(f"{region}는", f"{region}{particle}", 1)
            return {"ok": True, "reply": reply, "engine": "python-region-introduction",
                    "result": {"kind": "region_introduction", "region": region, "areas": areas}}
        query = re.sub(r"기타(?!어종)(?=\s|$|낚시|지수|지역|알려|보여)", "기타어종", text)
        if re.search(r"(?:^|\s)지수(?:가)?\s*(?:뭐야|뜻|의미|정의)", query):
            query = query.replace("지수", "예보지수", 1)
        payload = self._answer_internal(query)
        result = payload.get("result")
        # Numeric producer scores never enter user prose; ordering remains unchanged.
        if result and self._contains_producer_score(result):
            payload["reply"] = self._fallback_result_text(result)
        payload["reply"] = self._public_text(payload.get("reply", ""))
        return payload

    @staticmethod
    def _contains_producer_score(value: Any) -> bool:
        if isinstance(value, dict):
            return any("총점" in str(key) or (key == "metric" and item == "총점수")
                       or HybridOceanChatbot._contains_producer_score(item)
                       for key, item in value.items())
        if isinstance(value, list):
            return any(HybridOceanChatbot._contains_producer_score(item) for item in value)
        return False

    def _answer_internal(self, text: str) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            plan_started = time.monotonic()
            plan = self.planner.parse(text)
            plan_seconds = round(time.monotonic() - plan_started, 3)
        except Exception as exc:
            if good_fishing_levels(text):
                # The outage path must enforce the same grade constraint.
                slots = self.legacy.parse_slots(text)
                dates = []
                if slots.date_range:
                    start, end = slots.date_range
                    dates = [_fmt_ymd(start + timedelta(days=i)) for i in range((end - start).days + 1)]
                if re.search(r"내일\s+모레", text):
                    dates = [_fmt_ymd(self.base_date + timedelta(days=i)) for i in (1, 2)]
                elif "내일모레" in text:
                    dates = [_fmt_ymd(self.base_date + timedelta(days=2))]
                # "전체" in an all-results request does not mean the DY time slot.
                if slots.time == "DY" and not any(word in text for word in ["종일", "하루", "전일", "all day"]):
                    slots.time = None
                raw = {"operation": "detail", "filters": {"dates": dates, "regions": slots.regions or [],
                       "fish": slots.fish or [], "times": [slots.time] if slots.time else []}}
                if slots.metric and slots.threshold:
                    raw["numeric_conditions"] = [{"field": slots.metric, "operator": slots.threshold[0], "value": slots.threshold[1]}]
                plan = self.planner.validate(raw, text)
                result = self.query_engine.execute(plan)
                return {"ok": True, "reply": self._fallback_result_text(result), "plan": plan,
                        "result": result, "items": result.get("items", []), "engine": "python-rules-fallback",
                        "llm_error": str(exc), "timings": {"total_seconds": round(time.monotonic() - started, 3)}}
            fallback = self.legacy.answer(text)
            fallback["engine"] = "python-rules-fallback"
            fallback["llm_error"] = str(exc)
            fallback["timings"] = {"total_seconds": round(time.monotonic() - started, 3)}
            return fallback

        result = self.query_engine.execute(plan)
        broad_weather_lookup = bool(re.search(r"날씨|기상", text) and plan.get("environment_lookup") and result.get("kind") == "detail")
        exact_render = broad_weather_lookup or plan.get("hazard_lookup") or self._contains_producer_score(result) or bool(plan.get("good_fishing_recommendation")) or result.get("kind") in {"unsupported", "clarification"} or (
            result.get("kind") == "distinct" and plan.get("return_all")
        ) or (
            result.get("kind") == "detail" and plan.get("return_all") and not plan.get("environment_lookup")
        )
        try:
            answer_started = time.monotonic()
            if exact_render:
                reply = (("CSV에 제공되는 기온·수온·풍속·파고·유속 예보입니다.\n" + self._environment_result_text(result, date_once=True))
                         if broad_weather_lookup else self._fallback_result_text(result))
                answer_seconds = 0.0
                engine = f"{self.client.model} → Python CSV → exact renderer"
            else:
                reply = self._natural_answer(text, plan, result)
                if plan.get("environment_lookup") and not self._environment_answer_is_grounded(reply, result):
                    reply = self._environment_result_text(result)
                    engine = f"{self.client.model} → Python CSV → validated template"
                else:
                    engine = f"{self.client.model} → Python CSV → {self.client.model}"
                answer_seconds = round(time.monotonic() - answer_started, 3)
            llm_error = None
        except Exception as exc:
            reply = self._environment_result_text(result) if plan.get("environment_lookup") else self._fallback_result_text(result)
            answer_seconds = None
            engine = "LLM plan → Python CSV → template fallback"
            llm_error = str(exc)

        payload = {
            "ok": True,
            "reply": reply,
            "engine": engine,
            "plan": plan,
            "result": result,
            "items": result.get("items", []),
            "timings": {
                "plan_seconds": plan_seconds,
                "answer_seconds": answer_seconds,
                "total_seconds": round(time.monotonic() - started, 3),
            },
        }
        if llm_error:
            payload["llm_error"] = llm_error
        return payload


@lru_cache(maxsize=1)
def get_bot() -> HybridOceanChatbot:
    return HybridOceanChatbot(DATA_FILE)


class ChatHandler(BaseHTTPRequestHandler):
    server_version = "OceanChatbot/1.0"

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/index.html":
            html = UI_FILE.read_text(encoding="utf-8")
            self._send_html(html)
            return
        if parsed.path == "/api/meta":
            self._send_json({"ok": True, "meta": get_bot().meta()})
            return
        if parsed.path == "/api/health":
            self._send_json({"ok": True})
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/chat":
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return
        data = self._read_json()
        message = str(data.get("message", "")).strip()
        if not message:
            self._send_json({"ok": False, "error": "message is required"}, status=400)
            return
        bot = get_bot()
        result = bot.answer(message, context=data.get("context"))
        result["ok"] = True
        result["meta"] = bot.meta()
        self._send_json(result)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # Keep local terminal output readable.
        sys.stdout.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), fmt % args))


def main(argv: Sequence[str]) -> int:
    host = os.environ.get("OCEAN_HOST", "127.0.0.1")
    port = 8000
    if len(argv) > 1:
        try:
            port = int(argv[1])
        except ValueError:
            print(f"Invalid port: {argv[1]}")
            return 1

    if not DATA_FILE.exists():
        print(f"Missing CSV file: {DATA_FILE}")
        return 1
    if not UI_FILE.exists():
        print(f"Missing UI file: {UI_FILE}")
        return 1

    server = ThreadingHTTPServer((host, port), ChatHandler)
    print(f"Ocean chatbot running at http://{host}:{port}")
    print(f"CSV: {DATA_FILE.name}")
    print(f"Base date: {get_bot().base_date.isoformat()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
