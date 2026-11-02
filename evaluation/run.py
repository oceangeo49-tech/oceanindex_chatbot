#!/usr/bin/env python3
"""Run real /api/chat requests against an isolated server and the configured Ollama.
No mocked LLM. Never starts/stops the user's service. Exit 1 means failed checks.
"""
import argparse
import collections
import csv
import hashlib
import html
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ocean_chatbot_app as app
from evaluation.scenarios import build


CATEGORY_LABELS = {
    'advanced_v2':'추가 심화 질문 40개',
    'region_intro':'지역명만 입력', 'missing_conditions':'조회 조건 부족',
    'direct_environment':'날짜·지역·시간 환경 조회', 'paraphrase':'같은 의미의 다양한 표현',
    'conversation_region_first':'지역부터 시작하는 대화', 'conversation_topic_first':'항목부터 시작하는 대화',
    'conversation_date_change':'날짜 변경 후속 질문', 'conversation_time_change':'시간 변경 후속 질문',
    'sea_recommendation':'해역·권역별 추천', 'recommendation_count':'추천 개수 지정',
    'ranking':'최고·최저 지역 조회', 'explanation':'지수·어종 의미 설명',
    'unsupported':'서비스 범위 밖 질문', 'smalltalk':'인사·감정 표현',
    'out_of_range':'예보 범위 밖 날짜', 'unknown_region':'서비스에 없는 지역',
    'index_lookup':'바다낚시지수 조회', 'numeric_filter':'숫자 조건 필터',
    'exact_region_boundary':'이름이 겹치는 지역', 'multiple_regions':'여러 지역 동시 조회',
    'conversation_quoted_input':'따옴표·문장부호 대화', 'conversation_topic_change':'조회 항목 변경',
}


def source_rows():
    with app.DATA_FILE.open(encoding='cp949', newline='') as f:
        return list(csv.DictReader(f))


def check(response, expect, rows, history=None):
    failures = []
    def require(ok, code, message):
        if not ok:
            failures.append({'code':code, 'message':message})
    result = response.get('result') or {}
    plan = response.get('plan') or {}
    filters = plan.get('filters') or {}
    reply = response.get('reply','')
    items = result.get('items', response.get('items', []))
    kinds = expect.get('kind', [])
    kinds = [kinds] if isinstance(kinds,str) else kinds
    require(result.get('kind') in kinds, 'intent', f"expected {kinds}; got {result.get('kind')}")
    require(bool(reply.strip()), 'empty_reply', 'empty answer')
    require(not response.get('llm_error'), 'model_error', 'LLM call failed or response invalid')
    for word in ['총점수', '대표총점', '기타어종', '처리 경로:', *expect.get('forbid',[])]:
        require(word not in reply, 'presentation', f'forbidden wording: {word}')
    for word in expect.get('contains',[]):
        require(word in reply, 'presentation', f'missing text: {word}')
    if expect.get('no_items'):
        require(not items, 'unrequested_data', 'should ask/explain, not return rows')
    if 'term' in expect:
        require(plan.get('term') == expect['term'], 'intent', f"wrong explanation term: {plan.get('term')}")
    candidates = rows
    for key,column in [('dates','예측일자'),('regions','지역'),('areas','권역'),('times','시간')]:
        values = expect.get(key)
        if values is None:
            continue
        # Empty time selection means all slots; values in other fields are explicit constraints.
        require(set(filters.get(key,[])) == set(values), 'scope', f'{key}: {filters.get(key)} != {values}')
        if values:
            candidates = [r for r in candidates if (r[column][:4]+'-'+r[column][4:6]+'-'+r[column][6:] if key=='dates' else r[column]) in values]
    if 'levels' in expect:
        require(set(filters.get('forecast_indexes',[]))==set(expect['levels']), 'grade', 'missing/wrong grade filter')
        candidates = [r for r in candidates if r['예보지수'] in expect['levels']]
    for column, key in [('지역','excluded_regions'), ('어종','excluded_fish')]:
        if expect.get(key):
            candidates=[r for r in candidates if r[column] not in expect[key]]
    if expect.get('conditions'):
        operators={'<':lambda a,b:a<b,'<=':lambda a,b:a<=b,'>':lambda a,b:a>b,'>=':lambda a,b:a>=b,'==':lambda a,b:a==b}
        for condition in expect['conditions']:
            require(condition in plan.get('numeric_conditions',[]), 'numeric_filter', f'missing {condition}')
            candidates=[r for r in candidates if operators[condition['operator']](float(r[condition['field']]),condition['value'])]
        require(bool(items)==bool(candidates),'result_presence','numeric query omitted/invented matches')
    if expect.get('contains_any'):
        require(any(x in reply for x in expect['contains_any']), 'presentation', f"expected one of {expect['contains_any']}")
    if expect.get('unique_regions'):
        actual=[i.get('지역',i.get('group')) for i in items]
        require(len(actual)==len(set(actual)), 'duplicate_region','requested distinct places, returned repeated regions')
        wanted=min(len({r['지역'] for r in candidates}),expect['max_items'])
        require(len(actual)==wanted,'result_limit',f'expected {wanted} distinct places')
    if expect.get('optional_regions') and items:
        require(all(i.get('지역') in expect['optional_regions'] for i in items),'data_scope','outside regional group')
    if expect.get('previous_region_index') is not None and history:
        old=history[-1].get('response',{}).get('result',{}).get('items',[])
        names=list(dict.fromkeys(i.get('지역',i.get('group')) for i in old))
        index=expect['previous_region_index']
        if len(names)>index:
            require(filters.get('regions',response.get('context',{}).get('regions'))==[names[index]],'context','did not resolve previous ranked region')
    if expect.get('rank_n') or expect.get('all_ties'):
        groups=collections.defaultdict(list)
        for row in candidates:groups[row['지역']].append(float(row[expect['metric']]))
        ranked=sorted(((name,sum(values)/len(values)) for name,values in groups.items()),key=lambda x:x[1],reverse=expect['sort']=='desc')
        chosen=[pair for pair in ranked if abs(pair[1]-ranked[0][1])<1e-8] if expect.get('all_ties') else ranked[:expect['rank_n']]
        require({i.get('group') for i in items}=={name for name,_ in chosen},'ranking','wrong ranked regions or missing ties')
        expected_values=dict(chosen)
        require(all(i.get('value')==round(expected_values.get(i.get('group'),float('inf')),2) for i in items),'numeric_value','wrong ranked values')
    if 'numeric' in expect:
        condition=expect['numeric']
        require(condition in plan.get('numeric_conditions',[]), 'numeric_filter', f'missing {condition}')
        candidates=[r for r in candidates if float(r[condition['field']]) <= condition['value']]
    if 'max_items' in expect:
        require(len(items) <= expect['max_items'], 'result_limit', 'too many results')
    def matches(item, row):
        for key,value in item.items():
            if key not in row or key in ['group','metric','value']:
                continue
            expected = row[key]
            if key=='예측일자': expected=f'{expected[:4]}-{expected[4:6]}-{expected[6:]}'
            if str(value)!=str(expected): return False
        return True
    for item in items:
        if 'group' not in item:
            require(any(matches(item,row) for row in candidates), 'data_scope', f'row outside expected CSV subset: {item}')
        for field in expect.get('fields',[]):
            require(field in item, 'missing_field', f'missing {field}')
            if field in item and field != '어종':
                require(str(item[field]) in reply, 'answer_grounding', f'{field} value missing from answer')
    if expect.get('kind')=='detail':
        require(bool(items) == bool(candidates), 'result_presence', 'empty/unexpected result')
        if expect.get('fields'):
            columns=['예측일자','지역','시간',*expect['fields']]
            def key(row, csv_row=False):
                result=[]
                for column in columns:
                    value=str(row.get(column,''))
                    if csv_row and column=='예측일자': value=f'{value[:4]}-{value[4:6]}-{value[6:]}'
                    result.append(value)
                return tuple(result)
            expected_rows={key(row,True) for row in candidates}
            actual_rows={key(row) for row in items}
            require(actual_rows==expected_rows,'completeness','requested rows omitted, duplicated scope or invented values')
            numeric_fields=[f for f in expect['fields'] if f not in ['어종','예보지수']]
            if numeric_fields:
                known_numbers={float(row[f]) for row in candidates for f in numeric_fields}
                mentioned_numbers=re.findall(r'(-?\d+(?:\.\d+)?)\s*(?:℃|m/s|m(?=[\s,.입]))',reply)
                require(all(float(value) in known_numbers for value in mentioned_numbers),
                        'answer_grounding','unexplained measurement in answer')
    if expect.get('recommendation'):
        require(bool(items)==bool(candidates), 'result_presence', 'recommendation omitted or invented')
        scores=sorted([float(r['총점수']) for r in candidates],reverse=True)
        actual=[float(i['총점수']) for i in items if '총점수' in i]
        require(actual==scores[:min(len(scores),expect['max_items'])], 'ranking', 'recommendation scores not the correct top N')
    if expect.get('ranking'):
        groups=collections.defaultdict(list)
        for r in candidates: groups[r['지역']].append(float(r[expect['metric']]))
        values={r:sum(v)/len(v) for r,v in groups.items()}
        if values:
            best=(min if expect['sort']=='asc' else max)(values.values())
            valid={r for r,v in values.items() if abs(v-best)<1e-8}
            require(len(items)==1 and items[0].get('group') in valid,'ranking',f'expected extremum in {sorted(valid)}')
            if items:
                require(items[0].get('value')==round(best,2),'numeric_value','wrong aggregate value')
                require(str(items[0].get('value')) in reply,'answer_grounding','rank value missing')
    if expect.get('fields') and expect.get('regions'):
        allowed=set(expect['regions'])
        # Avoid substring false positives (e.g. a short name inside a longer region).
        mentioned={r['지역'] for r in rows if r['지역'] in reply}
        mentioned={r for r in mentioned if not any(r!=other and r in other for other in mentioned)}
        require(mentioned <= allowed,'answer_scope',f'unrequested regions in answer: {sorted(mentioned-allowed)}')
    return failures


def write_report(directory, records, metadata):
    totals=collections.defaultdict(lambda:{'total':0,'passed':0})
    engines=collections.Counter()
    failure_counts=collections.Counter()
    for record in records:
        entry=totals[record['category']]; entry['total']+=1; entry['passed']+=record['passed']
        for turn in record['turns']:
            engines[turn.get('response',{}).get('engine','error')]+=1
            failure_counts.update({f['code'] for f in turn['failures']})
    summary={**metadata,'completed':len(records),'passed':sum(r['passed'] for r in records),
             'failed':sum(not r['passed'] for r in records),'categories':dict(totals),
             'failure_types':dict(failure_counts),'engines':dict(engines),
             'llm_calls':sum(len(t.get('response',{}).get('_eval_llm_calls',[])) for r in records for t in r['turns'])}
    (directory/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    with (directory/'conversations.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer=csv.writer(f)
        writer.writerow(['시나리오', '유형', '반복 회차', '대화 순서', '질문', '실제 답변', '검사 결과', '실패 이유', '처리 경로', '응답 시간(초)'])
        for record in sorted(records, key=lambda r:(r['id'], r['trial'])):
            for index, turn in enumerate(record['turns'], 1):
                response=turn.get('response', {})
                writer.writerow([record['id'], CATEGORY_LABELS.get(record['category'], record['category']),
                    record['trial'], index, turn['question'], response.get('reply', ''),
                    '실패' if turn['failures'] else '통과',
                    ' / '.join(failure['message'] for failure in turn['failures']),
                    response.get('engine', ''), turn['seconds']])
    with (directory/'failures.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f); writer.writerow(['scenario','category','turn','question','failure','detail'])
        for r in records:
            for n,t in enumerate(r['turns'],1):
                for failure in t['failures']:
                    writer.writerow([r['id'],r['category'],n,t['question'],failure['code'],failure['message']])
    esc=lambda x:html.escape(str(x))
    details=[]
    for r in sorted(records,key=lambda r:(r['passed'],r['id'])):
        turns=[]
        for t in r['turns']:
            turns.append(f'<h4>{esc(t["question"])}</h4><pre>{esc(t.get("response",{}).get("reply",""))}</pre>'
                         f'<pre>{esc(json.dumps(t["failures"],ensure_ascii=False,indent=2))}</pre>'
                         f'<details><summary>기대 조건·조회 계획·모델 원문</summary><pre>{esc(json.dumps(t,ensure_ascii=False,indent=2))}</pre></details>')
        details.append(f'<details class="case {"pass" if r["passed"] else "fail"}"><summary>{"통과" if r["passed"] else "실패"} · {esc(r["id"])} · 반복 {r["trial"]} · {r["seconds"]:.2f}초</summary>{"".join(turns)}</details>')
    table=''.join(f'<tr><td>{esc(CATEGORY_LABELS.get(k,k))}</td><td>{v["passed"]}/{v["total"]}</td></tr>' for k,v in totals.items())
    page=f'''<!doctype html><html lang="ko"><meta charset="utf-8"><title>해양 챗봇 자동 평가</title>
<style>body{{background:#102633;color:#e9f4f7;font:16px system-ui;max-width:1100px;margin:40px auto;padding:20px}}h1{{color:#4bd9e6}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#091a24;padding:14px;border-radius:8px}}.case{{margin:12px 0;padding:14px;border:1px solid #49616a;border-radius:8px}}.fail{{border-color:#ef927f}}summary{{cursor:pointer}}td{{padding:5px 20px 5px 0}}button{{padding:10px;margin:10px 8px 10px 0}}</style>
<h1>해양 챗봇 자동 평가</h1><p>{summary['passed']} 통과 / {summary['failed']} 실패 / {len(records)}회 시나리오 실행</p>
<p>실제 Ollama 호출 {summary['llm_calls']}회 · 모델 {esc(metadata['model'])} · CSV 기준일 {esc(metadata['base_date'])}</p>
<p><a style="color:#4bd9e6" href="conversations.csv">전체 질문·실제 답변 CSV 열기</a></p>
<p>코드·데이터 기반 검사 결과입니다. 자연스러움과 의미 적합성은 별도 검토가 필요합니다. 미실행·중단 사례는 통과로 계산하지 않습니다.</p>
<table>{table}</table><button onclick="document.querySelectorAll('.pass').forEach(e=>e.hidden=!e.hidden)">통과 사례 숨기기/보기</button>
{''.join(details)}</html>'''
    (directory/'report.html').write_text(page)
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--repeat',type=int,default=1)
    parser.add_argument('--workers',type=int,default=1)
    parser.add_argument('--category',action='append')
    parser.add_argument('--only-failed',type=Path,help='previous results.jsonl')
    parser.add_argument('--limit',type=int)
    parser.add_argument('--catalog-only',action='store_true')
    args=parser.parse_args()
    if args.repeat<1 or not 1<=args.workers<=4: parser.error('repeat >= 1; workers 1..4')
    rows=source_rows(); cases=build(rows)
    root=ROOT/'evaluation'
    (root/'scenarios.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2))
    if args.category: cases=[c for c in cases if c['category'] in args.category]
    if args.only_failed:
        failed={json.loads(line)['id'] for line in args.only_failed.read_text().splitlines() if line and not json.loads(line)['passed']}
        cases=[c for c in cases if c['id'] in failed]
    if args.limit: cases=cases[:args.limit]
    if not cases: parser.error('no scenarios selected')
    print(f'{len(cases)} scenarios; {sum(len(c["turns"]) for c in cases)} questions per repeat',flush=True)
    if args.catalog_only:return 0
    directory=args.output or ROOT/'reports'/'runs'/datetime.now().strftime('%Y%m%d-%H%M%S')
    directory.mkdir(parents=True,exist_ok=True)
    if (directory/'results.jsonl').exists(): parser.error('output already has results; choose a new directory')
    bot=app.HybridOceanChatbot(app.DATA_FILE)
    status=bot.client.status()
    if not status.get('connected') or not status.get('available'):
        print('Configured Ollama model unavailable; refusing to report fallback-only results as a live evaluation.',file=sys.stderr);return 2
    app.get_bot=lambda:bot
    trace=threading.local()
    real_chat=bot.client.chat
    def traced_chat(system,user,json_mode=False,max_tokens=700):
        started=time.monotonic()
        call={'stage':'planner' if json_mode else 'narrator','input':user}
        try:
            result=real_chat(system,user,json_mode,max_tokens)
            call['output']=result;return result
        except Exception as e:
            call['error']=str(e);raise
        finally:
            call['seconds']=round(time.monotonic()-started,3)
            if hasattr(trace,'calls'):trace.calls.append(call)
    bot.client.chat=traced_chat
    class Handler(app.ChatHandler):
        def do_POST(self):
            trace.calls=[]
            return super().do_POST()
        def _send_json(self,payload,status=200):
            payload=dict(payload);payload['_eval_llm_calls']=getattr(trace,'calls',[])
            return super()._send_json(payload,status)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    endpoint=f'http://127.0.0.1:{server.server_port}/api/chat'
    metadata={'started':datetime.now(timezone.utc).isoformat(),'model':app.OLLAMA_MODEL,
              'base_date':bot.base_date.isoformat(),'source_sha256':hashlib.sha256((ROOT/'ocean_chatbot_app.py').read_bytes()).hexdigest(),
              'source_files':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ['ocean_chatbot_app.py','ocean_query_policy.py']},
              'csv_sha256':hashlib.sha256(app.DATA_FILE.read_bytes()).hexdigest(),
              'scenarios':len(cases),'planned_runs':len(cases)*args.repeat,'repeat':args.repeat,'workers':args.workers,
              'grader_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'scenarios_sha256':hashlib.sha256(json.dumps(cases,sort_keys=True,ensure_ascii=False).encode()).hexdigest()}
    (directory/'manifest.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2))
    (directory/'scenarios.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2))
    def run(case,trial):
        started=time.monotonic(); context={}; turns=[]
        for turn in case['turns']:
            response={}; start=time.monotonic()
            try:
                req=Request(endpoint,data=json.dumps({'message':turn['question'],'context':context}).encode(),headers={'Content-Type':'application/json'})
                with urlopen(req,timeout=max(180,app.LLM_TIMEOUT*3)) as r:response=json.load(r)
                failures=check(response,turn['expect'],rows,turns)
                context=response.get('context',{})
            except Exception as e:
                failures=[{'code':'exception','message':str(e)}]
            turns.append({**turn,'response':response,'failures':failures,'seconds':round(time.monotonic()-start,3)})
        return {'id':case['id'],'category':case['category'],'trial':trial,'turns':turns,
                'passed':not any(t['failures'] for t in turns),'seconds':round(time.monotonic()-started,3)}
    records=[]
    try:
        with (directory/'results.jsonl').open('w') as f, ThreadPoolExecutor(max_workers=args.workers) as pool:
            jobs=[pool.submit(run,c,trial) for trial in range(1,args.repeat+1) for c in cases]
            for job in as_completed(jobs):
                record=job.result();records.append(record)
                f.write(json.dumps(record,ensure_ascii=False)+'\n');f.flush()
                if not record['passed'] or len(records)%10==0:
                    codes=sorted({failure['code'] for t in record['turns'] for failure in t['failures']})
                    print(f'{len(records)}/{len(jobs)} {record["id"]} {"PASS" if record["passed"] else "FAIL " + ",".join(codes)}',flush=True)
                if len(records)%25==0:write_report(directory,records,metadata)
    finally:
        server.shutdown();server.server_close()
        summary=write_report(directory,records,metadata)
    from evaluation.history import export
    export()
    print(json.dumps({'passed':summary['passed'],'failed':summary['failed'],'llm_calls':summary['llm_calls'],'report':str(directory/'report.html')},ensure_ascii=False),flush=True)
    return 1 if summary['failed'] else 0

if __name__=='__main__':
    raise SystemExit(main())
