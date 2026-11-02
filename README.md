# 오프라인 해양예보 챗봇

사용자 질문을 Qwen3-8B가 JSON 조회 계획으로 변환하고, Python이 CSV를 조회·계산한 뒤 자연어 답변을 생성합니다.
지수 단계와 어종 특징 질문은 사용자 제공 원본 이미지를 정리한 `ocean_index_guide.json`을 참고합니다.

```text
사용자 질문
  -> Qwen3-8B 질문 해석
  -> JSON 계획 검증
  -> Python CSV 검색·계산
  -> Qwen3-8B 답변 설명
```

## 폴더·파일 안내

실행 경로를 유지하기 위해 기존 파일은 이동하지 않았습니다.

| 파일/폴더 | 역할 |
|---|---|
| `ocean_chatbot_app.py` | 서버, 질문 해석, 대화 조건 관리, CSV 조회, 답변 생성 |
| `ocean_query_policy.py` | 날짜·제외 조건·수치 조건 등의 계획 보정과 미지원 질문 안내 |
| `ocean_chatbot.html` | 사용자가 보는 챗봇 화면 |
| `SFEQ_INDEX_SERVICE_20250616.csv` | 조회·계산의 기준이 되는 예보 데이터 |
| `ocean_index_guide.json` | 지수 등급과 어종 특징 설명자료 |
| `test_ocean_chatbot.py` | 서버 처리 규칙에 대한 코드 테스트 |
| `offline_ocean_chatbot_deck_v2.html` | 발표자료 |
| `evaluation/` | 자동 평가 프로그램과 시험 문제 목록 |
| `reports/` | 실제 평가 실행 결과와 질문·답변 기록 |
| `__pycache__/` | Python이 자동 생성하는 실행 캐시. 직접 편집할 필요 없음 |

### 어떤 질문에 어떻게 답했는지 보는 곳

**앞으로는 [test_results.xlsx](reports/test_results.xlsx) 하나만 열면 됩니다.**
브라우저에서는 [결과 안내 화면](reports/index.html)을 열어도 됩니다.

| 엑셀 시트 | 볼 수 있는 내용 |
|---|---|
| 읽는법 | 기록의 의미와 주의할 점 |
| 실행요약 | 실행별 시나리오 수, 통과·실패 수, 실제 질문 수 |
| 최신질문답변 | 시나리오·대화 순서별 최근 질문과 답변, 실패 이유 (모든 반복은 전체질문답변 참조) |
| 전체질문답변 | 수정 전을 포함한 실제 질문·답변 전체 이력. 실행 폴더·판정 열로 필터 가능 |
| 수정이력 | 어떤 오류가 있었고 원인은 무엇이며 어떻게 고쳤는지 |

```bash
xdg-open /home/jy/oceanindex_chatbot/reports/test_results.xlsx
# 엑셀을 여는 프로그램이 없다면 브라우저 상세 보고서 사용
xdg-open /home/jy/oceanindex_chatbot/reports/index.html
```

실제 대화 평가가 끝나면 엑셀과 안내 화면은 자동 갱신됩니다. 기존 실행 기록은 덮어쓰지 않습니다.
수정 원인·조치 내역은 개발자가 `evaluation/fixes.json`에 기록합니다. 자동 시험이 코드를 자동 수정하는 구조는 아닙니다.
기록을 다시 모으기만 하려면 `python3 evaluation/history.py`를 실행하세요(LLM 호출 없음).

이 파일은 **자동 평가에서 보낸 질문·실제 답변**을 모은 것입니다. 평소 웹 사용자의 모든 대화를 수집하는 로그는 아닙니다.
현재 360개 시나리오, 1회당 451번의 질문입니다. 한 시나리오에 여러 차례 이어지는 대화가 포함될 수 있습니다.
‘통과’는 지정한 검사 조건을 만족했다는 뜻이며 모든 실제 질문에 대한 정확도를 의미하지 않습니다.
거리 추천·OR 조건 등 미지원 기능은 제한을 정확히 설명하면 통과하도록 구분합니다.

### evaluation 폴더

| 파일 | 역할 |
|---|---|
| `scenarios.py` | 시험 질문과 통과 조건을 정의하는 원본. 시험을 추가할 때 수정 |
| `scenarios_v2.py` | 추가 심화 질문 40개와 통과 조건 |
| `proposed_questions_v2.csv` | 추가 질문의 초안. 실제 결과는 종합 엑셀에서 확인 |
| `history.py` | 원본 실행 기록을 종합 엑셀과 안내 화면으로 묶음 |
| `fixes.json` | 개발자가 기록하는 오류 원인·수정 내역 |
| `scenarios.json` | 생성된 전체 질문 목록과 기대 조건. 실제 답변은 포함하지 않음 |
| `run.py` | 실제 API에 질문하고 CSV 기준으로 결과를 검사하며 보고서 저장 |
| `check.sh` | 코드 테스트 후 실제 대화 평가를 한 번에 실행 |
| `report.py` | 저장된 답변을 재채점하고 수정 전후 비교 보고서 생성. LLM 재호출 없음 |
| `test_evaluation.py` | 평가기 자체가 잘못된 지역·수치·빈 답변을 잡는지 검사 |

### reports 폴더

| 폴더 | 의미 |
|---|---|
| `baseline/` | 수정 전 최초 실행 원본. 292개 시나리오 중 28개 실패 |
| `verification/` | 수정 후 실제 재실행 원본. 320개 시나리오를 2회 반복 |
| `comparison/before/` | baseline 응답을 공통 검사 기준으로 재채점한 결과 |
| `comparison/after/` | verification 응답을 공통 검사 기준으로 재채점한 결과 |
| `comparison/index.html` | 수정 전후 비교 첫 화면 |
| `comparison/comparison.json` | 비교 결과를 프로그램에서 읽기 위한 파일 |
| `runs/날짜-시간/` | 이후 기본 명령으로 새 평가를 실행할 때 생성되는 결과 폴더 |

`comparison/before`, `comparison/after`는 새로 질문한 기록이 아닙니다. 기존 실행 원본을 재채점한 사본이므로 원본과 합산해서 실행 횟수를 세면 안 됩니다.

각 실행 폴더의 파일은 다음과 같습니다.

| 파일 | 볼 수 있는 내용 |
|---|---|
| `report.html` | 질문·실제 답변·통과/실패를 브라우저로 확인 |
| `conversations.csv` | 질문·답변을 한 줄씩 정리한 표. 시나리오, 회차, 대화 순서, 처리 시간 포함 |
| `results.jsonl` | 전체 원본 기록. 한 줄이 한 시나리오 실행이며 `turns`에 각 질문·응답이 들어 있음 |
| `scenarios.json` | 해당 실행에 사용한 질문과 통과 조건 |
| `failures.csv` | 실패한 검사만 정리한 표. 전부 통과하면 제목 행만 있음 |
| `summary.json` | 통과 수·실패 수·유형별 결과·실제 LLM 호출 수 |
| `manifest.json` | 모델·CSV 기준일·코드 및 데이터 해시 등 재현용 실행 정보 |

원본 JSONL에서 실제 질문은 `turns[].question`, 실제 답변은 `turns[].response.reply`,
기대 조건은 `turns[].expect`, 실패 사유는 `turns[].failures`에 있습니다.
LLM이 호출된 경우 `turns[].response._eval_llm_calls`에서 질문 해석/답변 작성 원문도 볼 수 있습니다.
정형 Python 답변은 이 호출 기록이 비어 있을 수 있습니다.

## 실행

Ollama와 모델을 준비합니다.

```bash
ollama pull qwen3:8b
```

챗봇 서버를 실행합니다.

```bash
python3 /home/jy/oceanindex_chatbot/ocean_chatbot_app.py
```

브라우저에서 `http://127.0.0.1:8000`을 엽니다.

8000번 포트를 이미 사용 중이면 다른 포트를 지정할 수 있습니다.

```bash
python3 /home/jy/oceanindex_chatbot/ocean_chatbot_app.py 8001
```

## 설정

기본 모델은 `qwen3:8b`입니다. 다른 Ollama 모델을 시험하려면 환경변수를 지정합니다.

```bash
OCEAN_LLM_MODEL=qwen3.6:35b python3 ocean_chatbot_app.py 8001
```

Ollama 주소와 요청 제한 시간도 변경할 수 있습니다.

```bash
OCEAN_OLLAMA_URL=http://127.0.0.1:11434 OCEAN_LLM_TIMEOUT=120 python3 ocean_chatbot_app.py
```

## 테스트

```bash
python3 -m unittest -v test_ocean_chatbot.py
```

LLM이 없거나 호출에 실패하면 기존 Python 규칙 분석기로 자동 전환합니다. 전체 목록과 지원 불가능 질문은 명칭 누락이나 환각을 막기 위해 Python의 정확 출력 경로를 사용합니다.

어종의 서식·조과 설명은 공식 자료의 원문을 보존하되, 답변에서는 일반적인 경향과 참고정보로 표현하며 실제 조과를 보장하지 않습니다.


## 좋은 낚시 지역 추천 기준

`내일 낚시하기 좋은지역`은 예보지수 **좋음·매우좋음**만 조회하고 총점수 내림차순으로 표시합니다. 날짜·지역·시간·어종·지수를 보여주며(총점수는 내부 정렬에만 사용), 기본 표시 개수는 5건입니다. `전부/모두/전체`를 요청하면 전체 조회합니다(상세 출력 상한 200건). 기본 건수는 지역·시간·어종 조합의 수입니다. `3곳` 또는 `중복 없이`를 요청하면 지역별 대표 시간대·어종 하나씩 선택해 중복 지역을 제거합니다. 조건에 맞는 지역이 요청 수보다 적으면 그 사실을 안내합니다.

`매우 좋은 지역`은 매우좋음만 조회합니다. 조건에 맞는 자료가 없으면 다른 등급으로 조건을 완화하지 않습니다. LLM이 필터를 누락하거나 연결에 실패해도 같은 등급 조건을 적용합니다. 일반적인 `예보지수 알려줘` 조회에는 이 추천 필터를 적용하지 않습니다.

`울릉도 낚시하기 어때?`처럼 지역의 전반적인 낚시 여건을 물으면 좋은 등급만 찾지 않고 모든 지수를 시간대별로 요약합니다. `낚시하기 좋은 지역 추천해줘`처럼 추천을 명시한 질문은 좋음·매우좋음만 찾는 동작을 유지합니다.

`낚시하기 위험한 곳` 질문은 어종별 지수 목록으로 답하지 않습니다. Python이 날짜·지역·시간대별로 중복 어종 행을 합치고, 해당 조회 범위 안에서 평균풍속과 평균파고의 상대 순위를 계산해 상위 5건과 실제 값·예상 범위를 보여줍니다. 이는 조회 후보 사이의 상대 비교이며 공식 안전 판정이나 출조 가능 여부를 뜻하지 않습니다. 출조 전 최신 해상특보와 현장 상황을 확인하도록 안내합니다.

날짜는 **CSV 생산일 기준**입니다. 기준일이 2025-06-16이면 `내일`은 2025-06-17, `4일 뒤`는 2025-06-20입니다. 날짜만 덧붙이는 후속 질문은 직전 대화의 지역·항목을 이어서 조회합니다.


## 실제 대화 자동 평가

```bash
# 기존 서비스는 건드리지 않고 임시 로컬 HTTP 서버로 검사합니다.
# 실제로 설정된 Ollama 모델을 호출하므로 Ollama가 실행 중이어야 합니다.
python3 evaluation/run.py

# 출력 변동을 확인하기 위해 모든 시나리오를 3번씩 반복합니다.
python3 evaluation/run.py --repeat 3 --workers 2

# 특정 유형만 검사
python3 evaluation/run.py --category sea_recommendation --repeat 3

# 이전 실행에서 실패한 시나리오만 재검사
python3 evaluation/run.py --only-failed reports/실행폴더/results.jsonl

# 실제 질문 목록과 기대 조건만 생성 (모델 호출 없음)
python3 evaluation/run.py --catalog-only

# 코드 테스트와 대화 평가를 순서대로 실행
bash evaluation/check.sh --repeat 3 --workers 2
```

`evaluation/scenarios.py`는 질문과 기대 조건의 원본이며, 실행 시 검토 가능한
`evaluation/scenarios.json`을 생성합니다. CSV 생산일을 읽어 상대 날짜를 계산합니다.
지역·권역·시간·등급·측정값·추천 순위는 CSV를 직접 읽는 별도 검사기로 확인합니다.
실제 LLM 원문과 보정된 조회 계획도 기록하므로, 질문 해석과 조회/출력 오류를 구분할 수 있습니다.
정답은 챗봇의 기존 답변을 복사해서 만들지 않습니다.

결과 원본은 `reports/runs/날짜-시간/`에 저장되며, `reports/test_results.xlsx`와 `reports/index.html`이 함께 갱신됩니다.

- `report.html`: 브라우저에서 보는 결과표, 실패 질문과 실제 답변, 상세 기록
- `summary.json`: 유형별 통과율, 실패 분류, 실제 LLM 호출 수
- `failures.csv`: 실패 항목을 표 계산 프로그램에서 확인
- `results.jsonl`: 모든 대화 단계, 기대 조건, 응답, LLM 원문, 소요 시간
- `manifest.json`: 모델명, 코드·CSV·시나리오 해시, 반복 수
- `scenarios.json`: 해당 실행에 사용한 질문 목록과 기대 조건

대화마다 맥락을 분리합니다. 실제 서비스의 8000번 포트를 재시작하거나 점유하지 않습니다.
요청 동시 실행 기본값은 1이며, `--workers 2`는 실제 서비스와 GPU 자원을 공유하므로
운영 중에는 여유 시간에 실행하는 편이 좋습니다. 모델 연결이 없으면 평가를 중단합니다.
실행 중 요청 실패도 실패로 기록하며, 정형 Python 경로와 실제 LLM 호출 횟수를 구분합니다.

프로세스 종료 코드는 통과 0, 검사 실패 1, 설정 문제 2입니다.
자연스러움·모든 문장의 의미 정확성까지 자동 검사로 보장하지는 않습니다.
HTML 답변을 표본 검토하고, 새로운 오류는 시나리오에 추가한 뒤 수정 전후를 비교하세요.
평가기는 서비스 코드를 임의로 수정하지 않습니다. 실패를 재현·수정한 뒤 같은 평가를 다시 실행합니다.

수정 전후 결과를 비교할 때는 같은 검사기로 저장된 응답을 재채점할 수 있습니다.
이 명령은 모델을 다시 호출하지 않으며, 원본 실행 기록도 변경하지 않습니다.

```bash
python3 evaluation/report.py --baseline reports/baseline --current reports/verification --output reports/comparison
```

비교 보고서는 공통 시나리오의 개선/퇴행과 추가 시나리오를 구분하며,
반복 중 한 번이라도 실패한 시나리오는 성공으로 표시하지 않습니다.
