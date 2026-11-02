"""Reviewable scenarios; expected values come from CSV, not chatbot-generated answers."""
from datetime import datetime, timedelta

METRICS = {
    '수온': ['최소수온', '평균수온', '최대수온'],
    '풍속': ['최소풍속', '평균풍속', '최대풍속'],
    '파고': ['최소파고', '평균파고', '최대파고'],
    '기온': ['최저기온', '평균기온', '최대기온'],
    '유속': ['최소유속', '평균유속', '최대유속'],
}

def build(rows):
    base = datetime.strptime(max(r['생산일'] for r in rows), '%Y%m%d').date()
    day = lambda n: (base + timedelta(days=n)).isoformat()
    cases = []
    def add(category, turns):
        cases.append({'id': f'{category}-{sum(c["category"] == category for c in cases)+1:03d}',
                      'category': category, 'turns': [{'question': q, 'expect': e} for q, e in turns]})
    def detail(region, date, topic, times=None):
        return {'kind': 'detail', 'regions': [region], 'dates': [date],
                'times': times or [], 'fields': METRICS[topic]}
    for r in sorted({r['지역'] for r in rows}):
        add('region_intro', [(r, {'kind': 'region_introduction', 'contains': [r, '바다낚시지수']})])
    for topic in ['수온', '풍속', '바람', '파고', '파도', '기온', '유속', '바다낚시지수']:
        for suffix in ['', ' 알려줘']:
            add('missing_conditions', [(topic+suffix, {'kind': 'clarification', 'no_items': True})])
    regions = ['하조도', '가거도', '울릉도', '영흥도', '포항', '김녕']
    for r in regions:
        for topic in METRICS:
            for phrase, offset, times in [('내일 오전', 1, ['AM']), ('모레 오후', 2, ['PM'])]:
                add('direct_environment', [(f'{phrase} {r} {topic} 알려줘', detail(r, day(offset), topic, times))])
    templates = ['{r} 내일 오후 {m} 어떻게 돼?', '내일 오후에 {r} {m} 알려주세요',
                 '{r}의 내일 오후 {m}은?', '내일 오후 {r} {m} 좀 알려줘',
                 '내일 오후 {r} {m} 조회해줘', '“내일 오후 {r} {m} 알려줘”']
    for r in ['하조도', '가거도', '울릉도']:
        for m in ['수온', '풍속']:
            for template in templates:
                add('paraphrase', [(template.format(r=r,m=m), detail(r,day(1),m,['PM']))])
    for r in ['하조도','가거도','울릉도','영흥도']:
        for topic in ['수온','풍속','파고']:
            add('conversation_region_first', [(r, {'kind':'region_introduction'}),
                (topic, {'kind':'clarification','no_items':True}),
                ('내일 오후', detail(r,day(1),topic,['PM']))])
            add('conversation_topic_first', [(topic, {'kind':'clarification'}),
                ('모레', {'kind':'clarification'}), (r, detail(r,day(2),topic))])
            add('conversation_date_change', [(f'내일 오후 {r} {topic}', detail(r,day(1),topic,['PM'])),
                ('모레는?', detail(r,day(2),topic,['PM']))])
    seas = {'동해':['동해중부','동해남부'], '동해중부':['동해중부'], '동해남부':['동해남부'],
            '서해':['황해중부','황해남부'], '황해':['황해중부','황해남부'],
            '남해':['남해동부','남해서부'], '남해동부':['남해동부'], '제주도':['제주도']}
    for sea, areas in seas.items():
        for when, offset in [('내일',1),('모레',2)]:
            for wording, levels in [('낚시하기 좋은 지역', ['좋음','매우좋음']),
                                    ('지수가 매우 좋은 지역', ['매우좋음'])]:
                add('sea_recommendation', [(f'{when} {sea}에서 {wording} 알려줘',
                    {'kind':['detail','no_match'],'areas':areas,'dates':[day(offset)],
                     'levels':levels,'recommendation':True,'max_items':5})])
    for count in [1,3,5,10]:
        for sea, areas in [('동해',seas['동해']),('서해',seas['서해'])]:
            add('recommendation_count', [(f'내일 {sea}에서 낚시하기 좋은 곳 {count}개 알려줘',
                {'kind':['detail','no_match'],'areas':areas,'dates':[day(1)],
                 'levels':['좋음','매우좋음'],'recommendation':True,'max_items':count})])
    for when, offset in [('내일',1),('모레',2)]:
        for metric, phrase, order in [('평균풍속','바람이 가장 약한','asc'),('평균파고','파도가 가장 낮은','asc'),
                                      ('평균수온','수온이 가장 높은','desc')]:
            for time_name, slot in [('오전','AM'),('오후','PM')]:
                add('ranking', [(f'{when} {time_name} {phrase} 지역 알려줘',
                    {'kind':'ranking','dates':[day(offset)],'times':[slot], 'metric':metric,
                     'sort':order,'max_items':1,'ranking':True,'forbid':['표시 한도']})])
    for question, term in [('낚시 지수가 뭐야?','예보지수'),('바다낚시지수란?','예보지수'),
                           ('매우좋음은 무슨 뜻이야?','매우좋음'),('좋음 등급의 의미를 알려줘','좋음'),
                           ('보통은 어떤 단계야?','보통'),('나쁨은 어떤 뜻이야?','나쁨'),
                           ('매우나쁨의 의미 알려줘','매우나쁨'),('참돔 특징 알려줘','참돔'),
                           ('감성돔 특징 알려줘','감성돔'),('농어는 어떤 어종이야?','농어')]:
        add('explanation', [(question, {'kind':'explain','term':term,'no_items':True})])
    for question in ['워싱턴의 수도','서울 맛집 알려줘','부산 숙박비 알려줘','하조도 가는 버스 요금',
                     '내일 하조도 실제 어획량','가거도에서 실제로 몇 마리 잡혔어?',
                     '오늘 주식 추천해줘','내 컴퓨터 비밀번호 알려줘']:
        add('unsupported', [(question, {'kind':['unsupported','clarification'],'no_items':True})])
    for question in ['안녕','고마워','내일 낚시하기 싫어','오늘은 쉬고 싶어','낚시 가기 귀찮아','잘 가']:
        add('smalltalk', [(question, {'kind':['conversation','clarification'],'no_items':True,
            'forbid':['분석 결과','총 34','수온, 유속, 파고']})])
    for r in regions:
        add('out_of_range', [(f'2099-01-01 {r} 수온 알려줘',
            {'kind':'no_match','no_items':True,'dates':['2099-01-01'],'regions':[r]})])
    for r in ['아틀란티스','없는섬','서울']:
        for m in ['수온','풍속']:
            add('unknown_region', [(f'내일 {r} {m} 알려줘',
                {'kind':['unsupported','clarification','no_match'],'no_items':True})])
    for r in ['하조도','가거도','울릉도','영흥도']:
        for when,offset in [('내일',1),('모레',2)]:
            add('index_lookup', [(f'{when} {r} 바다낚시지수 알려줘',
                {'kind':'detail','regions':[r],'dates':[day(offset)],'fields':['어종','예보지수']})])
    for sea,areas in [('동해',seas['동해']),('서해',seas['서해'])]:
        for metric, wording, threshold in [('평균풍속','평균풍속 5m/s 이하',5),('평균파고','평균파고 1m 이하',1)]:
            for when,offset in [('내일',1),('모레',2)]:
                add('numeric_filter', [(f'{when} {sea} {wording}인 지역 모두 알려줘',
                    {'kind':['detail','distinct','no_match'],'areas':areas,'dates':[day(offset)],
                     'numeric':{'field':metric,'operator':'<=','value':threshold}})])
    # Short follow-ups must not silently reset the topic or invent a destination.
    for r in ['하조도','가거도','울릉도']:
        for topic in ['수온','풍속']:
            add('conversation_time_change', [(f'내일 오전 {r} {topic}',detail(r,day(1),topic,['AM'])),
                ('오후는?',detail(r,day(1),topic,['PM']))])
    for r in ['포항', '방포항']:
        for topic in METRICS:
            add('exact_region_boundary', [(f'내일 오후 {r} {topic}', detail(r,day(1),topic,['PM']))])
    for a,b in [('가거도','거문도'),('포항','방포항')]:
        for topic in ['수온','풍속','파고']:
            expected=detail(a,day(1),topic,['PM']); expected['regions']=[a,b]
            add('multiple_regions', [(f'내일 오후 {a}와 {b} {topic} 알려줘', expected)])
    for r in ['하조도','가거도','울릉도']:
        for topic in ['수온','풍속']:
            add('conversation_quoted_input', [(f'“{r}”',{'kind':'region_introduction'}),
                (f'{topic}?',{'kind':'clarification'}),('모레 오후는?',detail(r,day(2),topic,['PM']))])
    for r in ['하조도','가거도','울릉도']:
        for topic in ['수온','풍속']:
            add('conversation_topic_change', [(f'내일 오후 {r} 파고',detail(r,day(1),'파고',['PM'])),
                (topic+'은?',detail(r,day(1),topic,['PM']))])
    from evaluation.scenarios_v2 import build_v2
    cases.extend(build_v2(rows))
    return cases
