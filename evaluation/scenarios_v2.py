"""Forty harder cases reviewed separately from the original regression suite."""
import csv
from pathlib import Path
from datetime import datetime,timedelta

def build_v2(rows):
    base=datetime.strptime(max(r['생산일'] for r in rows),'%Y%m%d').date()
    d=lambda offset:(base+timedelta(days=offset)).isoformat()
    east=['동해중부','동해남부']; south=['남해동부','남해서부']
    def rec(areas=None,**kw):
        x={'kind':['detail','no_match'],'dates':[d(1)],'levels':['좋음','매우좋음'],'max_items':5}
        if areas is not None:x['areas']=areas
        x.update(kw);return x
    def env(region,offset=1,metric='수온',times=None):
        fields={'수온':['최소수온','평균수온','최대수온'],'풍속':['최소풍속','평균풍속','최대풍속']}[metric]
        return {'kind':'detail','regions':[region],'dates':[d(offset)],'times':times or [],'fields':fields}
    decline={'kind':['unsupported','clarification','no_match'],'no_items':True}
    def numeric(conditions,**kw):
        x={'kind':['detail','distinct','no_match'],'dates':[d(1)],'conditions':conditions};x.update(kw);return x
    c=lambda field,op,value:{'field':field,'operator':op,'value':value}
    expectations={
      1:rec(['동해남부'],unique_regions=True,max_items=3),
      2:rec(east+south),3:rec(east),4:{**decline,'contains':['동해시']},
      5:{'kind':['detail','clarification'],'optional_regions':['부산남부','부산동부','부산서부']},
      6:{**decline,'contains_any':['거리','근처','인근']},
      7:{'kind':'ranking','dates':[d(1)],'metric':'평균풍속','sort':'asc','rank_n':3,'excluded_regions':['가거도']},
      8:rec(excluded_fish=['기타어종']),9:rec(east,levels=['좋음']),
      10:numeric([c('평균풍속','<=',5),c('평균파고','<=',1)],areas=east,times=['PM']),
      11:{**decline,'contains_any':['또는','이거나','OR']},
      12:{'kind':'no_match','no_items':True},
      13:numeric([c('평균풍속','==',5)]),14:numeric([c('평균풍속','<',5)]),15:numeric([c('평균풍속','<=',5)]),
      16:rec(east,unique_regions=True,max_items=3),17:rec(unique_regions=True,max_items=100,contains_any=['지역','곳']),
      18:{'kind':['clarification','explain'],'no_items':True,'contains_any':['추천','순서','기준']},
      19:{'kind':'ranking','dates':[d(1)],'times':['PM'],'metric':'평균풍속','sort':'asc','all_ties':True},
      20:{'kind':'ranking','regions':['가거도','하조도'],'dates':[d(1)],'times':['PM'],'metric':'평균풍속','sort':'asc','rank_n':1},
      21:{'kind':['clarification','compare','ranking'],'contains_any':['시간','평균','오전','오후']},
      22:env('하조도',2,times=['PM']),23:{**env('하조도',times=['PM']),'dates':[d(1),d(2)]},
      24:{**decline,'contains_any':['날짜','존재']},25:{'kind':'detail','regions':['하조도'],'dates':[d(i) for i in range(7) if (base+timedelta(days=i)).weekday()>=5]},
      26:{'kind':['explain','clarification'],'contains':['2025-06-16'],'contains_any':['기준','생산']},
      27:[env('가거도',times=['PM']),env('하조도',times=['PM'])],
      28:[env('가거도',times=['PM']),env('가거도',metric='풍속',times=['PM'])],
      29:[env('가거도',times=['PM']),env('가거도',2,times=['PM'])],
      30:[env('가거도',times=['PM']),{'kind':['conversation','clarification'],'no_items':True},
          {'kind':'clarification','no_items':True,'contains_any':['지역','날짜']}],
      31:[rec(east,unique_regions=True,max_items=3),{'kind':['detail','clarification'],'previous_region_index':1}],
      32:[{'kind':'region_introduction'},{'kind':'explain','no_items':True,'contains_any':['온도','수온']}],
      33:env('하조도',metric='풍속'),34:{'kind':'conversation','no_items':True},
      35:env('가거도',metric='풍속',times=['PM']),36:env('하조도'),
      37:{'kind':['explain','clarification'],'contains_any':['보장하지','보장하는','반드시 잡히','보장할']},
      38:{**decline,'contains_any':['안전','판단'],'forbid':['무조건 안전합니다','안전하게 출항하세요']},
      39:{'kind':['clarification','unsupported','explain'],'no_items':True,'contains':['하조도','황해']},
      40:{**decline,'contains_any':['만들','실제','임의']},
    }
    with (Path(__file__).parent/'proposed_questions_v2.csv').open(encoding='utf-8-sig',newline='') as f:
        catalog=list(csv.DictReader(f))
    cases=[]
    for index,row in enumerate(catalog,1):
        questions=row['질문 또는 대화 순서'].split(' → ')
        ex=expectations[index]; ex=ex if isinstance(ex,list) else [ex]
        assert len(questions)==len(ex),(index,questions,ex)
        cases.append({'id':row['ID'],'category':'advanced_v2','topic':row['유형'],
                      'criteria_note':row['통과 기준 초안'],
                      'turns':[{'question':q,'expect':e} for q,e in zip(questions,ex)]})
    return cases
