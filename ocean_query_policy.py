"""Deterministic constraints around an LLM plan. No model or CSV facts are invented here."""
import re
from datetime import date,timedelta

METRICS={'수온':('최소수온','평균수온','최대수온'), '풍속':('최소풍속','평균풍속','최대풍속'),
         '파고':('최소파고','평균파고','최대파고'), '기온':('최저기온','평균기온','최대기온'),
         '유속':('최소유속','평균유속','최대유속')}

def dates_in(text,base):
    explicit=re.search(r'(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})',text)
    if explicit:
        try:return [date(*(int(x) for x in explicit.groups())).isoformat()]
        except ValueError:return None
    if '주말' in text:
        return [(base+timedelta(days=i)).isoformat() for i in range(7) if (base+timedelta(days=i)).weekday()>=5]
    if '내일모레' in text:return [(base+timedelta(days=2)).isoformat()]
    relative=re.search(r'(\d+)\s*일\s*(뒤|후|전)(?:에)?',text)
    if relative:
        offset=int(relative.group(1)) * (-1 if relative.group(2)=='전' else 1)
        return [(base+timedelta(days=offset)).isoformat()]
    offsets=[]
    for word,offset in [('오늘',0),('내일',1),('모레',2),('글피',3)]:
        if word in text:offsets.append(offset)
    return [(base+timedelta(days=i)).isoformat() for i in sorted(set(offsets))]

def normalize(text):
    text=re.sub(r'(?<!\S)낼(?=\s|$)','내일',text)
    text=text.replace('알려주세여','알려주세요').replace('얼만큼 불어','알려줘')
    return text

def preflight(text,bot):
    def message(kind,reply):return {'ok':True,'reply':reply,'result':{'kind':kind},'engine':'python-request-policy','context':{}}
    if dates_in(text,bot.base_date) is None:
        return message('clarification','존재하지 않는 날짜입니다. 날짜를 다시 확인해 주세요.')
    if '동해시' in text and '동해시' not in bot.regions:
        return message('clarification','동해시 자체의 예보는 현재 제공하지 않습니다. 동해 해역의 서비스 지역 중 어느 곳이 궁금하신가요?')
    if re.search(r'근처|인근|주변',text):
        return message('clarification','현재 자료에는 지역 사이의 거리 정보가 없어 근처 지역을 정확히 고를 수 없습니다. 조회할 지역이나 해역을 지정해 주세요.')
    if re.search(r'출항|출조',text) and re.search(r'안전|무조건|괜찮',text):
        return message('unsupported','풍속이나 바다낚시지수만으로 출항 안전을 판단할 수 없습니다. 최신 해상 기상과 특보, 현장 통제 여부 등을 함께 확인해야 합니다.')
    if re.search(r'적당히|아무렇게|임의로',text) and re.search(r'만들|지어',text):
        return message('unsupported','예보 값을 임의로 만들어 드릴 수는 없습니다. 실제 제공된 데이터만 안내해 드릴게요.')
    if (re.search(r'관광|여행|가족|랜드마크|해양\s*스포츠|힐링|산책|해수욕', text)
            and re.search(r'추천|장소|지역|곳|어디', text)
            and not re.search(r'낚시|어종|조과|출조|잡기|잡을|바다낚시', text)):
        return message('unsupported','현재 자료는 바다낚시지수와 해양 환경 예보만 제공합니다. 관광·여행·레저 장소는 추천할 수 없습니다.')
    if re.search(r'낚시.*(?:불가능|못\s*하|안\s*되).*(?:대체|다른)\s*활동', text):
        return message('unsupported','현재 자료에는 낚시 대신 할 수 있는 관광·레저 활동 정보가 없습니다. 바다낚시지수와 해양 환경 예보만 안내할 수 있습니다.')
    if (re.search(r'낚시|출조', text)
            and re.search(r'(?:하기|가기|출조)\s*(?:전|전에)|반드시\s*확인|주의\s*사항|준비\s*사항', text)):
        return message('explain','출조 전에는 최신 해상특보와 현장 통제 여부를 먼저 확인하고, 조회할 지역과 날짜의 풍속·파고 등 해양 환경을 함께 확인해 주세요. 지수는 참고 정보이며 실제 조과나 안전을 보장하지 않습니다.')
    if (re.search(r'조황|지수', text)
            and re.search(r'안\s*좋|나쁨|나쁜', text)
            and re.search(r'낚시.*(?:안|못|불가능)|어디서도', text)):
        return message('explain','지수가 나쁨이라고 해서 모든 지역에서 낚시가 불가능하다는 뜻은 아닙니다. 지수는 해당 날짜·지역·어종의 상대적인 여건을 나타내는 참고 정보이며, 실제 출조 가능 여부는 해상특보와 현장 통제를 따로 확인해야 합니다.')
    if (re.search(r'낚시', text)
            and re.search(r'어떤\s*날씨|날씨.*(?:가장\s*)?좋', text)):
        return message('explain','낚시에 가장 좋은 날씨를 하나의 조건으로 단정할 수는 없습니다. 지역과 날짜를 알려주시면 제공된 풍속·파고·수온·기온·유속과 바다낚시지수를 안내해 드릴게요. 출조 전에는 최신 해상특보와 현장 상황을 우선 확인해 주세요.')
    broad_fishing_place = bool(
        re.search(r'낚시.*(?:하기\s*좋|할\s*만한|할만한|좋은\s*(?:곳|지역|장소)|(?:곳|지역|장소)\s*(?:있|알려|추천)|어디)', text)
        or re.search(r'어디.*낚시.*(?:잘\s*잡|좋|가능)', text)
    )
    if broad_fishing_place and not re.search(r'그곳|그\s*지역', text) and not dates_in(text, bot.base_date):
        if re.search(r'잘\s*잡|조과|어획', text):
            return message('clarification','지수는 실제로 잘 잡히는 곳이나 어획량을 보장하지 않습니다. 추천을 조회할 날짜와 원하는 어종 또는 해역을 알려주세요.')
        return message('clarification','언제 낚시할 예정인가요? 오늘, 내일처럼 날짜를 알려주시면 지수가 좋은 지역을 찾아드릴게요.')
    if re.search(r'왜|어째서',text) and re.search(r'20\d{2}년|기준일|날짜',text):
        return message('explain',f'이 챗봇은 현재 달력이 아니라 CSV 생산일 {bot.base_date.isoformat()}을 기준으로 오늘·내일을 계산합니다. 최신 예보를 보려면 데이터 갱신이 필요합니다.')
    if re.search(r'왜.*(?:먼저|순서|추천)',text):
        return message('clarification','어떤 추천 결과의 순서가 궁금하신가요? 추천은 조건에 맞는 지수를 고른 뒤 내부 추천 기준으로 정렬합니다. 실제 어획량의 순서는 아닙니다.')
    if '매우좋음' in text and re.search(r'반드시|무조건|보장',text):
        return message('explain','매우좋음은 지수 기준상 낚시에 유리한 조건이라는 뜻이며, 물고기가 반드시 잡힌다는 것을 보장하지 않습니다.')
    if re.search(r'이거나|또는|\bOR\b',text) and re.search(r'\d',text):
        return message('clarification','현재 여러 수치 조건은 모두 충족하는 경우를 조회합니다. “또는(OR)” 조건은 아직 지원하지 않습니다. 조건을 나누어 조회해 주세요.')
    for term in METRICS:
        if term in text and re.search(r'뭐야|무엇|뜻|의미|정의',text):
            meanings={'수온':'바닷물의 온도','풍속':'바람이 부는 속도','파고':'파도의 높이','기온':'공기의 온도','유속':'물이 흐르는 속도'}
            return message('explain',f'{term}은 {meanings[term]}를 뜻합니다. 특정 지역의 예보가 궁금하면 지역과 날짜를 알려주세요.')
    # Correct a stated geographic premise rather than force-fitting the requested sea.
    if re.search(r'있으니까|있으니|에 있으',text):
        for region in bot._parse_regions(text) or []:
            areas=sorted({r['권역'] for r in bot.rows if r['지역']==region})
            for sea,prefix in [('동해','동해'),('서해','황해'),('남해','남해')]:
                if sea in text and not any(a.startswith(prefix) for a in areas):
                    return message('clarification',f'{region}는 데이터상 {"·".join(areas)} 권역에 속합니다. {sea} 추천에 임의로 넣을 수는 없습니다. 해당 지역을 별도로 조회할까요?')
    return None

def refine_plan(plan,text,bot):
    f=plan['filters']; available_areas=sorted({r['권역'] for r in bot.rows})
    excluded={'areas':[],'regions':[],'fish':[],'forecast_indexes':[]}
    remaining=text
    for key,values in [('areas',available_areas+['동해','서해','황해','남해']),('regions',bot.regions),
                       ('fish',bot.fish),('forecast_indexes',['매우좋음','매우나쁨','좋음','보통','나쁨'])]:
        for value in sorted(values,key=len,reverse=True):
            pattern=re.escape(value)+r'(?:\s*어종)?(?:은|는|을|를)?\s*(?:말고|빼고|제외)'
            if re.search(pattern,remaining):
                actual=[a for a in available_areas if a.startswith('황해' if value=='서해' else value)] if key=='areas' else [value]
                excluded[key].extend(actual)
                remaining=re.sub(pattern,' ',remaining)
    # Canonical expansion applies after excluded names have been removed.
    areas=set(); area_text=remaining
    for area in sorted(available_areas,key=len,reverse=True):
        if area in area_text:areas.add(area);area_text=area_text.replace(area,' ')
    for name,prefix in [('동해','동해'),('서해','황해'),('황해','황해'),('남해','남해')]:
        if name in area_text:areas.update(a for a in available_areas if a.startswith(prefix))
    if areas or excluded['areas']:f['areas']=sorted(areas-set(excluded['areas']))
    if excluded['regions']:f['regions']=[r for r in bot._parse_regions(remaining) or [] if r not in excluded['regions']]
    if excluded['fish']:f['fish']=[r for r in f.get('fish',[]) if r not in excluded['fish']]
    if excluded['forecast_indexes']:f['forecast_indexes']=[v for v in f.get('forecast_indexes',[]) if v not in excluded['forecast_indexes']]
    plan['exclude_filters']={k:sorted(set(v)) for k,v in excluded.items() if v}
    dates=dates_in(text,bot.base_date)
    if dates:f['dates']=dates
    # User-written numeric operators are authoritative, including == and carried fields.
    aliases={field:field for values in METRICS.values() for field in values}
    aliases.update({name:values[1] for name,values in METRICS.items()});aliases.update({'바람':'평균풍속','파도':'평균파고'})
    field_pattern='|'.join(re.escape(x) for x in sorted(aliases,key=len,reverse=True))
    tokens=re.compile(r'(?P<field>'+field_pattern+r')|(?P<exact>정확히\s*)?(?P<number>\d+(?:\.\d+)?)\s*(?:m/s|m|℃|도)?\s*(?P<op>이하|미만|이상|초과|이내)?')
    last=None; conditions=[]
    for m in tokens.finditer(text):
        if m.group('field'):last=aliases[m.group('field')];continue
        if last and (m.group('op') or m.group('exact')):
            op={'이하':'<=','미만':'<','이상':'>=','초과':'>','이내':'<='}.get(m.group('op'),'==')
            conditions.append({'field':last,'operator':op,'value':float(m.group('number'))})
    if conditions:
        plan['numeric_conditions']=conditions
        if not re.search(r'가장|제일|순위|비교',text):
            plan.update(operation='detail',select=['예측일자','지역','시간',*dict.fromkeys(c['field'] for c in conditions)],
                        limit=None,return_all=True,environment_lookup=False,clarification_needed=False,
                        good_fishing_recommendation=False)
    fishing_context = bool(
        re.search(r'낚시|어종|조과|출조|잡기|잡을|바다낚시|(?:예보)?지수', remaining)
        or any(fish in remaining for fish in bot.fish)
    )
    good_request = fishing_context and (
        plan.get('good_fishing_recommendation')
        or (('추천' in remaining or '좋음만' in remaining) and not conditions)
    )
    if good_request and plan['operation'] != 'unsupported' and not re.search(r'뜻|의미|왜|안전',text):
        levels=['좋음'] if '좋음만' in remaining and '매우좋음만' not in remaining else (['매우좋음'] if re.search(r'매우\s*좋(?:은|음)',remaining) else ['좋음','매우좋음'])
        f['forecast_indexes']=[level for level in levels if level not in excluded['forecast_indexes']]
        plan.update(operation='detail',select=['예측일자','지역','시간','어종','총점수','예보지수'],
                    good_fishing_recommendation=True,environment_lookup=False,clarification_needed=False)
        count=re.search(r'(\d+)\s*(개|곳|건)',text)
        if count:plan['limit']=min(200,max(1,int(count.group(1))))
        elif not any(w in text for w in ['모두','전체','전부']):plan['limit']=5
        if (count and count.group(2)=='곳') or '중복 없이' in text:
            plan['unique_regions']=True
    if not conditions and re.search(r'바람.*약한|풍속.*(?:낮은|약한)|파도.*낮은|수온.*높은',text):
        if re.search(r'지역|곳|중',text):
            named_regions=bot._parse_regions(remaining) or []
            if named_regions:f['regions']=named_regions
            metric='평균수온' if '수온' in text else ('평균파고' if '파도' in text else '평균풍속')
            plan.update(operation='ranking',metric=metric,group_by='지역',aggregation='average',sort='desc' if '높은' in text else 'asc',clarification_needed=False)
            count=re.search(r'(\d+)\s*(개|곳)',text)
            plan['limit']=int(count.group(1)) if count else 1
            plan['all_ties']=bool(re.search(r'모두|동률|동점',text))
    # "낚시하기 위험한 곳" asks about rough environmental conditions, not
    # poor fish-specific index rows. Keep an explicit date/area/time scope, but
    # remove fish and index dimensions and let Python rank wind/wave conditions.
    if re.search(r'위험|거친\s*(?:바다|파도)|파도.*(?:높은|거센)|바람.*(?:강한|센)', text) \
            and re.search(r'낚시|지역|곳|어디|해역', text):
        if not f.get('dates'):
            f['dates']=[(bot.base_date+timedelta(days=1)).isoformat()]
        f['fish']=[]
        f['fish_types']=[]
        f['forecast_indexes']=[]
        for key in ('fish','forecast_indexes'):
            plan.get('exclude_filters',{}).pop(key,None)
        plan.update(operation='detail',
                    select=['예측일자','지역','시간','평균풍속','최대풍속','평균파고','최대파고'],
                    group_by=None, metric=None, sort=None, limit=5, return_all=False,
                    environment_lookup=False, good_fishing_recommendation=False,
                    hazard_lookup=True, clarification_needed=False, clarification_question=None)
    # A general "how is fishing there?" question requests an outlook, not only
    # the good/very-good subset used by explicit recommendation questions.
    outlook_request = bool(re.search(r'낚시(?:하기|가)?\s*어때|낚시.*괜찮아|출조.*어때', text))
    if outlook_request and plan['operation'] != 'unsupported':
        if not f.get('dates'):
            f['dates']=[(bot.base_date+timedelta(days=1)).isoformat()]
        f['forecast_indexes']=[]
        plan.get('exclude_filters',{}).pop('forecast_indexes',None)
        plan.update(operation='detail',
                    select=['예측일자','지역','시간','어종','예보지수'],
                    group_by=None, metric=None, sort=None, limit=None, return_all=True,
                    environment_lookup=False, good_fishing_recommendation=False,
                    fishing_outlook_lookup=True)
        if not f.get('regions') and not f.get('areas'):
            plan.update(clarification_needed=True,
                        clarification_question='어느 지역의 낚시 여건이 궁금하신가요? 지역 이름을 알려주세요.')
        else:
            plan.update(clarification_needed=False, clarification_question=None)
    return plan
