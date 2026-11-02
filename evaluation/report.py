#!/usr/bin/env python3
"""Re-grade saved responses and compare matching scenarios without calling the LLM."""
import argparse
import hashlib
import html
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from evaluation.run import check, source_rows, write_report


def rescore(source, target):
    if target.exists():
        raise ValueError(f'Refusing to overwrite {target}')
    target.mkdir(parents=True)
    manifest=json.loads((source/'manifest.json').read_text())
    from ocean_chatbot_app import DATA_FILE
    csv_hash=hashlib.sha256(DATA_FILE.read_bytes()).hexdigest()
    if csv_hash != manifest['csv_sha256']:
        raise ValueError('CSV changed since this run; restore the recorded data before rescoring.')
    rows=source_rows()
    manifest['response_source']=str(source.resolve())
    manifest['grader_sha256']=hashlib.sha256((ROOT/'evaluation/run.py').read_bytes()).hexdigest()
    records=[]
    for line in (source/'results.jsonl').read_text().splitlines():
        record=json.loads(line)
        for ti, turn in enumerate(record['turns']):
            if turn.get('response'):
                turn['failures']=check(turn['response'],turn['expect'],rows,record['turns'][:ti])
        record['passed']=not any(t['failures'] for t in record['turns'])
        records.append(record)
    (target/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    (target/'scenarios.json').write_text((source/'scenarios.json').read_text())
    (target/'results.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records))
    write_report(target,records,manifest)
    return records


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline',type=Path,required=True)
    p.add_argument('--current',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    before=rescore(args.baseline,args.output/'before')
    after=rescore(args.current,args.output/'after')
    def outcomes(records):
        result={}
        for r in records:result[r['id']]=result.get(r['id'],True) and r['passed']
        return result
    b,a=outcomes(before),outcomes(after)
    before_cases={c['id']:c for c in json.loads((args.baseline/'scenarios.json').read_text())}
    after_cases={c['id']:c for c in json.loads((args.current/'scenarios.json').read_text())}
    common={key for key in set(b)&set(a) if before_cases[key] == after_cases[key]}
    changed=(set(b)&set(a))-common
    summary={'baseline_scenarios':len(b),'current_scenarios':len(a),'common_scenarios':len(common),'changed_scenarios_not_compared':sorted(changed),
             'baseline_passed':sum(b.values()),'current_passed_all_repeats':sum(a.values()),
             'fixed':sorted(k for k in common if not b[k] and a[k]),
             'regressed':sorted(k for k in common if b[k] and not a[k]),
             'still_failed':sorted(k for k in common if not b[k] and not a[k]),
             'new_failed':sorted(k for k in set(a)-set(b) if not a[k]),
             'current_runs':len(after),'current_passed_runs':sum(r['passed'] for r in after)}
    (args.output/'comparison.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    esc=html.escape
    report=f'''<!doctype html><html lang="ko"><meta charset="utf-8"><title>해양 챗봇 평가 비교</title>
<style>body{{font:18px system-ui;background:#102633;color:#edf4f8;max-width:1000px;margin:60px auto;padding:25px}}a,h1{{color:#42d4e4}}td,th{{padding:15px;border-bottom:1px solid #456}}pre{{white-space:pre-wrap}}.box{{padding:20px;background:#1d3947;border-radius:12px}}</style>
<h1>해양 챗봇 자동 평가 — 수정 전후 비교</h1>
<p>동일한 CSV와 기대 조건으로 실제 API 응답을 검사했습니다. 현재 결과는 모든 반복에서 통과한 시나리오만 성공으로 계산합니다.</p>
<table><tr><th></th><th>수정 전</th><th>수정 후</th></tr><tr><td>시나리오 수</td><td>{len(b)}</td><td>{len(a)}</td></tr>
<tr><td>모든 실행 통과</td><td>{sum(b.values())}</td><td>{sum(a.values())}</td></tr>
<tr><td>한 번 이상 실패</td><td>{len(b)-sum(b.values())}</td><td>{len(a)-sum(a.values())}</td></tr></table>
<p class="box">공통 {len(common)}개 중 개선 {len(summary['fixed'])}개 · 새로 실패 {len(summary['regressed'])}개 · 계속 실패 {len(summary['still_failed'])}개</p>
<p>수정 후 실행: {len(after)}회 중 {summary['current_passed_runs']}회 통과</p>
<p><a href="before/report.html">수정 전 상세 보고서</a> · <a href="after/report.html">수정 후 상세 보고서</a></p>
<p>평가 대상 밖의 질문과 모든 자연어 표현에 대한 무오류를 보장하지 않습니다. 정형 경로와 실제 모델 호출 수는 상세 보고서에 구분되어 있습니다.</p>
<details><summary>비교 원문</summary><pre>{esc(json.dumps(summary,ensure_ascii=False,indent=2))}</pre></details></html>'''
    (args.output/'index.html').write_text(report)
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
