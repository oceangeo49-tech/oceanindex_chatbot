#!/usr/bin/env python3
"""Rebuild the single user-facing Excel workbook from original run records (stdlib only)."""
import json
import os
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'

def column(n):
    out = ''
    while n:
        n, r = divmod(n-1, 26)
        out = chr(65+r) + out
    return out

def write_xlsx(path, sheets):
    temp = path.with_suffix('.tmp.xlsx')
    with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED) as z:
        overrides = ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(sheets)+1))
        z.writestr('[Content_Types].xml', f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>{overrides}</Types>')
        z.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml', f'<workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'+''.join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i,(name,_,_) in enumerate(sheets,1))+'</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1))+f'<Relationship Id="rId{len(sheets)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr('xl/styles.xml', f'<styleSheet xmlns="{NS}"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="2"><xf fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf fontId="1" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        for i, (name, rows, widths) in enumerate(sheets, 1):
            body = []
            for rn, row in enumerate(rows,1):
                cells=[]
                for cn,value in enumerate(row,1):
                    ref=f'{column(cn)}{rn}'
                    if isinstance(value,(int,float)):
                        cells.append(f'<c r="{ref}" s="{int(rn==1)}"><v>{value}</v></c>')
                    else:
                        text=''.join(c for c in str(value or '') if c in '\n\r\t' or ord(c)>=32)[:32760]
                        cells.append(f'<c r="{ref}" s="{int(rn==1)}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>')
                body.append(f'<row r="{rn}">'+''.join(cells)+'</row>')
            cols=''.join(f'<col min="{n}" max="{n}" width="{w}" customWidth="1"/>' for n,w in enumerate(widths,1))
            z.writestr(f'xl/worksheets/sheet{i}.xml', f'<worksheet xmlns="{NS}"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols>{cols}</cols><sheetData>'+''.join(body)+f'</sheetData><autoFilter ref="A1:{column(len(rows[0]))}{len(rows)}"/></worksheet>')
    os.replace(temp,path)

def export():
    reports=ROOT/'reports'
    runs=[]
    for file in reports.rglob('results.jsonl'):
        # A rescored comparison is a copy, not another execution.
        if 'comparison' in file.relative_to(reports).parts: continue
        manifest=file.parent/'manifest.json'
        if not manifest.exists(): continue
        meta=json.loads(manifest.read_text())
        if meta.get('response_source'): continue
        records=[json.loads(line) for line in file.read_text().splitlines() if line.strip()]
        runs.append((meta.get('started',''), file.parent, meta, records))
    runs.sort(key=lambda r:(r[0],str(r[1])))
    overview=[['실행 폴더','실행 시작(UTC)','완료 시나리오 회차','계획 회차','통과','실패','질문 수','LLM 호출 수','상태']]
    qa=[['실행 폴더','시나리오 ID','유형','반복 회차','대화 순서','질문','실제 답변','판정','실패 이유','통과 조건(요약)','통과 조건(JSON)','응답 초','처리 경로']]
    latest={}
    for started,path,meta,records in runs:
        label=str(path.relative_to(reports))
        overview.append([label,started,len(records),meta.get('planned_runs',len(records)),sum(r['passed'] for r in records),sum(not r['passed'] for r in records),sum(len(r['turns']) for r in records),sum(len(t.get('response',{}).get('_eval_llm_calls',[])) for r in records for t in r['turns']),'완료' if len(records)==meta.get('planned_runs',len(records)) else '진행 중 또는 중단'])
        catalog_file=path/'scenarios.json'
        catalog={c['id']:c for c in json.loads(catalog_file.read_text())} if catalog_file.exists() else {}
        for record in sorted(records,key=lambda r:(r['id'],r['trial'])):
            for ti,turn in enumerate(record['turns'],1):
                response=turn.get('response',{})
                criteria=catalog.get(record['id'],{}).get('criteria_note','')
                if not criteria:
                    labels={'kind':'응답 유형','regions':'지역','dates':'날짜','times':'시간','areas':'권역','levels':'지수','fields':'출력 항목','max_items':'최대 개수','metric':'비교 항목'}
                    criteria=' / '.join(f'{label}: {turn["expect"][key]}' for key,label in labels.items() if key in turn['expect'])
                    for key, text in {'detail':'상세 조회', 'clarification':'확인 질문', 'unsupported':'지원 범위 안내', 'explain':'설명', 'conversation':'일반 대화', 'no_match':'결과 없음', 'ranking':'순위', 'region_introduction':'지역 소개'}.items():
                        criteria=criteria.replace(key,text)
                    if turn['expect'].get('no_items'): criteria += ' / 조회 행 없이 확인 질문 또는 설명'
                    if turn['expect'].get('unique_regions'): criteria += ' / 지역 중복 없음'
                row=[label,record['id'],record['category'],record['trial'],ti,turn['question'],response.get('reply',''),'실패' if turn['failures'] else '통과',' / '.join(f['message'] for f in turn['failures']),criteria,json.dumps(turn['expect'],ensure_ascii=False),turn.get('seconds',''),response.get('engine','')]
                qa.append(row)
                latest[(record['id'],ti)]=row
    fixes_file=ROOT/'evaluation/fixes.json'
    fixes=json.loads(fixes_file.read_text()) if fixes_file.exists() else []
    fixrows=[['날짜','대상 시나리오·유형','문제','원인','수정 내용','확인 실행·제약']]+[[x.get(k,'') for k in ['date','cases','problem','cause','change','verification']] for x in fixes]
    guide=[['항목','설명'],['갱신 시각(UTC)',datetime.now(timezone.utc).isoformat()],['먼저 볼 시트','실행요약 → 최신질문답변 → 수정이력. 과거 실제 답변은 전체질문답변에서 실행 폴더로 필터하세요.'],['최신질문답변','시나리오·대화 순서별 가장 최근 실행의 마지막 반복 응답입니다. 모든 반복 결과는 전체질문답변에 있습니다. 실행 폴더가 서로 다를 수 있습니다.'],['통과의 뜻','정해둔 자동 검사 조건을 만족했다는 뜻입니다. 모든 표현이 자연스럽다거나 실서비스 정확도가 100%라는 뜻은 아닙니다.'],['미지원 질문','지원하지 않는 OR 조건, 거리 추천 등은 적절히 제한을 알리면 통과합니다. 기능 구현 완료와 구분해야 합니다.'],['시나리오와 질문','여러 차례 주고받는 대화는 1개 시나리오이며 질문은 여러 개입니다. 반복 시험 회차도 별도로 기록합니다.'],['수정이력','원인 분석과 수정 내용은 개발자가 기록합니다. 테스트가 자동으로 코드를 고치는 것은 아닙니다.'],['기준일','CSV 생산일 2025-06-16 기준입니다. 내일=2025-06-17을 유지합니다.'],['원본','각 실행 폴더 results.jsonl에 응답·구조화 계획·LLM 호출 기록이 있습니다. comparison은 기존 답변 재채점본이므로 합산에서 제외합니다.']]
    target=reports/'test_results.xlsx'
    widths=[40,20,25,12,12,60,95,12,65,65,65,12,28]
    write_xlsx(target,[('읽는법',guide,[25,120]),('실행요약',overview,[45,30,20,15,12,12,12,15,25]),('최신질문답변',[qa[0]]+[latest[k] for k in sorted(latest)],widths),('전체질문답변',qa,widths),('수정이력',fixrows,[18,35,60,65,70,60])])
    import html
    entries=''.join(f'<li><a href="{html.escape(str(p.relative_to(reports)))}/report.html">{html.escape(str(p.relative_to(reports)))}</a></li>' for _,p,_,_ in reversed(runs))
    (reports/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>챗봇 테스트 결과</title><style>body{font:18px sans-serif;max-width:1000px;margin:50px auto;line-height:1.8}a{color:#087ba0}</style><h1>챗봇 테스트 결과</h1><p><a href="test_results.xlsx">test_results.xlsx 다운로드</a> — 질문, 실제 답변, 실패 이유, 수정 이력을 한 파일에서 확인하세요.</p><p>엑셀의 ‘실행요약’에서 전체 결과, ‘최신질문답변’에서 최근 답변, ‘수정이력’에서 변경 사항을 확인합니다. 통과율은 정해진 시험 조건에 한정됩니다.</p><h2>상세 실행 기록 (최신순)</h2><ul>'+entries+'</ul>')
    print(target)
    return target

if __name__=='__main__': export()
