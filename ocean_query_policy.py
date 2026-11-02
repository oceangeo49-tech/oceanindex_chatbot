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

def _message(kind, reply):
    return {'ok': True, 'reply': reply, 'result': {'kind': kind},
            'engine': 'python-request-policy', 'context': {}}


def _invalid_date_rule(text, bot):
    if dates_in(text, bot.base_date) is None:
        return _message('clarification', '존재하지 않는 날짜입니다. 날짜를 다시 확인해 주세요.')


def _unsupported_location_rule(text, bot):
    if '동해시' in text and '동해시' not in bot.regions:
        return _message('clarification', '동해시 자체의 예보는 현재 제공하지 않습니다. 동해 해역의 서비스 지역 중 어느 곳이 궁금하신가요?')
    if re.search(r'근처|인근|주변', text):
        return _message('clarification', '현재 자료에는 지역 사이의 거리 정보가 없어 근처 지역을 정확히 고를 수 없습니다. 조회할 지역이나 해역을 지정해 주세요.')


def _safety_claim_rule(text, bot):
    if re.search(r'출항|출조', text) and re.search(r'안전|무조건|괜찮', text):
        return _message('unsupported', '풍속이나 바다낚시지수만으로 출항 안전을 판단할 수 없습니다. 최신 해상 기상과 특보, 현장 통제 여부 등을 함께 확인해야 합니다.')
    if re.search(r'적당히|아무렇게|임의로', text) and re.search(r'만들|지어', text):
        return _message('unsupported', '예보 값을 임의로 만들어 드릴 수는 없습니다. 실제 제공된 데이터만 안내해 드릴게요.')


def _outside_service_rule(text, bot):
    tourism = re.search(r'관광|여행|가족|랜드마크|해양\s*스포츠|힐링|산책|해수욕', text)
    place_request = re.search(r'추천|장소|지역|곳|어디', text)
    fishing = re.search(r'낚시|어종|조과|출조|잡기|잡을|바다낚시', text)
    if tourism and place_request and not fishing:
        return _message('unsupported', '현재 자료는 바다낚시지수와 해양 환경 예보만 제공합니다. 관광·여행·레저 장소는 추천할 수 없습니다.')
    if re.search(r'낚시.*(?:불가능|못\s*하|안\s*되).*(?:대체|다른)\s*활동', text):
        return _message('unsupported', '현재 자료에는 낚시 대신 할 수 있는 관광·레저 활동 정보가 없습니다. 바다낚시지수와 해양 환경 예보만 안내할 수 있습니다.')


def _fishing_guidance_rule(text, bot):
    if (re.search(r'낚시|출조', text)
            and re.search(r'(?:하기|가기|출조)\s*(?:전|전에)|반드시\s*확인|주의\s*사항|준비\s*사항', text)):
        return _message('explain', '출조 전에는 최신 해상특보와 현장 통제 여부를 먼저 확인하고, 조회할 지역과 날짜의 풍속·파고 등 해양 환경을 함께 확인해 주세요. 지수는 참고 정보이며 실제 조과나 안전을 보장하지 않습니다.')
    if (re.search(r'조황|지수', text)
            and re.search(r'안\s*좋|나쁨|나쁜', text)
            and re.search(r'낚시.*(?:안|못|불가능)|어디서도', text)):
        return _message('explain', '지수가 나쁨이라고 해서 모든 지역에서 낚시가 불가능하다는 뜻은 아닙니다. 지수는 해당 날짜·지역·어종의 상대적인 여건을 나타내는 참고 정보이며, 실제 출조 가능 여부는 해상특보와 현장 통제를 따로 확인해야 합니다.')
    if re.search(r'낚시', text) and re.search(r'어떤\s*날씨|날씨.*(?:가장\s*)?좋', text):
        return _message('explain', '낚시에 가장 좋은 날씨를 하나의 조건으로 단정할 수는 없습니다. 지역과 날짜를 알려주시면 제공된 풍속·파고·수온·기온·유속과 바다낚시지수를 안내해 드릴게요. 출조 전에는 최신 해상특보와 현장 상황을 우선 확인해 주세요.')


def _missing_recommendation_date_rule(text, bot):
    broad_place = bool(
        re.search(r'낚시.*(?:하기\s*좋|할\s*만한|할만한|좋은\s*(?:곳|지역|장소)|(?:곳|지역|장소)\s*(?:있|알려|추천)|어디)', text)
        or re.search(r'어디.*낚시.*(?:잘\s*잡|좋|가능)', text)
    )
    if not broad_place or re.search(r'그곳|그\s*지역', text) or dates_in(text, bot.base_date):
        return None
    if re.search(r'잘\s*잡|조과|어획', text):
        return _message('clarification', '지수는 실제로 잘 잡히는 곳이나 어획량을 보장하지 않습니다. 추천을 조회할 날짜와 원하는 어종 또는 해역을 알려주세요.')
    return _message('clarification', '언제 낚시할 예정인가요? 오늘, 내일처럼 날짜를 알려주시면 지수가 좋은 지역을 찾아드릴게요.')


def _explanation_rule(text, bot):
    if re.search(r'왜|어째서', text) and re.search(r'20\d{2}년|기준일|날짜', text):
        return _message('explain', f'이 챗봇은 현재 달력이 아니라 CSV 생산일 {bot.base_date.isoformat()}을 기준으로 오늘·내일을 계산합니다. 최신 예보를 보려면 데이터 갱신이 필요합니다.')
    if re.search(r'왜.*(?:먼저|순서|추천)', text):
        return _message('clarification', '어떤 추천 결과의 순서가 궁금하신가요? 추천은 조건에 맞는 지수를 고른 뒤 내부 추천 기준으로 정렬합니다. 실제 어획량의 순서는 아닙니다.')
    if '매우좋음' in text and re.search(r'반드시|무조건|보장', text):
        return _message('explain', '매우좋음은 지수 기준상 낚시에 유리한 조건이라는 뜻이며, 물고기가 반드시 잡힌다는 것을 보장하지 않습니다.')
    if re.search(r'이거나|또는|\bOR\b', text) and re.search(r'\d', text):
        return _message('clarification', '현재 여러 수치 조건은 모두 충족하는 경우를 조회합니다. “또는(OR)” 조건은 아직 지원하지 않습니다. 조건을 나누어 조회해 주세요.')
    meanings = {'수온': '바닷물의 온도', '풍속': '바람이 부는 속도', '파고': '파도의 높이',
                '기온': '공기의 온도', '유속': '물이 흐르는 속도'}
    for term, meaning in meanings.items():
        if term in text and re.search(r'뭐야|무엇|뜻|의미|정의', text):
            return _message('explain', f'{term}은 {meaning}를 뜻합니다. 특정 지역의 예보가 궁금하면 지역과 날짜를 알려주세요.')


def _geography_premise_rule(text, bot):
    if not re.search(r'있으니까|있으니|에 있으', text):
        return None
    for region in bot._parse_regions(text) or []:
        areas = sorted({row['권역'] for row in bot.rows if row['지역'] == region})
        for sea, prefix in [('동해', '동해'), ('서해', '황해'), ('남해', '남해')]:
            if sea in text and not any(area.startswith(prefix) for area in areas):
                return _message('clarification', f'{region}는 데이터상 {"·".join(areas)} 권역에 속합니다. {sea} 추천에 임의로 넣을 수는 없습니다. 해당 지역을 별도로 조회할까요?')


# Earlier rules have higher priority.  Adding a rule requires a name and a
# deliberate position instead of another anonymous branch in preflight().
PREFLIGHT_RULES = (
    ('invalid_date', _invalid_date_rule),
    ('unsupported_location', _unsupported_location_rule),
    ('safety_claim', _safety_claim_rule),
    ('outside_service', _outside_service_rule),
    ('fishing_guidance', _fishing_guidance_rule),
    ('missing_recommendation_date', _missing_recommendation_date_rule),
    ('explanation', _explanation_rule),
    ('geography_premise', _geography_premise_rule),
)


def preflight(text, bot):
    for _name, rule in PREFLIGHT_RULES:
        response = rule(text, bot)
        if response:
            return response
    return None

def _apply_scope_filters(plan, text, bot):
    filters = plan['filters']
    available_areas = sorted({row['권역'] for row in bot.rows})
    excluded = {'areas': [], 'regions': [], 'fish': [], 'forecast_indexes': []}
    remaining = text
    values_by_filter = [
        ('areas', available_areas + ['동해', '서해', '황해', '남해']),
        ('regions', bot.regions), ('fish', bot.fish),
        ('forecast_indexes', ['매우좋음', '매우나쁨', '좋음', '보통', '나쁨']),
    ]
    for key, values in values_by_filter:
        for value in sorted(values, key=len, reverse=True):
            pattern = re.escape(value) + r'(?:\s*어종)?(?:은|는|을|를)?\s*(?:말고|빼고|제외)'
            if re.search(pattern, remaining):
                prefix = '황해' if value == '서해' else value
                actual = [area for area in available_areas if area.startswith(prefix)] if key == 'areas' else [value]
                excluded[key].extend(actual)
                remaining = re.sub(pattern, ' ', remaining)

    areas = set()
    area_text = remaining
    for area in sorted(available_areas, key=len, reverse=True):
        if area in area_text:
            areas.add(area)
            area_text = area_text.replace(area, ' ')
    for name, prefix in [('동해', '동해'), ('서해', '황해'), ('황해', '황해'), ('남해', '남해')]:
        if name in area_text:
            areas.update(area for area in available_areas if area.startswith(prefix))
    if areas or excluded['areas']:
        filters['areas'] = sorted(areas - set(excluded['areas']))
    if excluded['regions']:
        filters['regions'] = [region for region in bot._parse_regions(remaining) or [] if region not in excluded['regions']]
    if excluded['fish']:
        filters['fish'] = [fish for fish in filters.get('fish', []) if fish not in excluded['fish']]
    if excluded['forecast_indexes']:
        filters['forecast_indexes'] = [level for level in filters.get('forecast_indexes', []) if level not in excluded['forecast_indexes']]
    plan['exclude_filters'] = {key: sorted(set(values)) for key, values in excluded.items() if values}
    parsed_dates = dates_in(text, bot.base_date)
    if parsed_dates:
        filters['dates'] = parsed_dates
    return remaining, excluded


def _numeric_conditions(text):
    aliases = {field: field for values in METRICS.values() for field in values}
    aliases.update({name: values[1] for name, values in METRICS.items()})
    aliases.update({'바람': '평균풍속', '파도': '평균파고'})
    field_pattern = '|'.join(re.escape(value) for value in sorted(aliases, key=len, reverse=True))
    tokens = re.compile(r'(?P<field>' + field_pattern + r')|(?P<exact>정확히\s*)?(?P<number>\d+(?:\.\d+)?)\s*(?:m/s|m|℃|도)?\s*(?P<op>이하|미만|이상|초과|이내)?')
    last_field = None
    conditions = []
    operators = {'이하': '<=', '미만': '<', '이상': '>=', '초과': '>', '이내': '<='}
    for match in tokens.finditer(text):
        if match.group('field'):
            last_field = aliases[match.group('field')]
            continue
        if last_field and (match.group('op') or match.group('exact')):
            conditions.append({'field': last_field,
                               'operator': operators.get(match.group('op'), '=='),
                               'value': float(match.group('number'))})
    return conditions


def _apply_numeric_plan(plan, text, conditions):
    if not conditions:
        return
    plan['numeric_conditions'] = conditions
    if not re.search(r'가장|제일|순위|비교', text):
        fields = list(dict.fromkeys(condition['field'] for condition in conditions))
        plan.update(operation='detail', select=['예측일자', '지역', '시간', *fields],
                    limit=None, return_all=True, environment_lookup=False,
                    clarification_needed=False, good_fishing_recommendation=False)


def _apply_good_recommendation(plan, text, remaining, excluded, conditions, bot):
    fishing_context = bool(
        re.search(r'낚시|어종|조과|출조|잡기|잡을|바다낚시|(?:예보)?지수', remaining)
        or any(fish in remaining for fish in bot.fish)
    )
    requested = fishing_context and (
        plan.get('good_fishing_recommendation')
        or (('추천' in remaining or '좋음만' in remaining) and not conditions)
    )
    if not requested or plan['operation'] == 'unsupported' or re.search(r'뜻|의미|왜|안전', text):
        return
    if '좋음만' in remaining and '매우좋음만' not in remaining:
        levels = ['좋음']
    elif re.search(r'매우\s*좋(?:은|음)', remaining):
        levels = ['매우좋음']
    else:
        levels = ['좋음', '매우좋음']
    plan['filters']['forecast_indexes'] = [level for level in levels if level not in excluded['forecast_indexes']]
    plan.update(operation='detail', select=['예측일자', '지역', '시간', '어종', '총점수', '예보지수'],
                good_fishing_recommendation=True, environment_lookup=False, clarification_needed=False)
    count = re.search(r'(\d+)\s*(개|곳|건)', text)
    if count:
        plan['limit'] = min(200, max(1, int(count.group(1))))
    elif not any(word in text for word in ['모두', '전체', '전부']):
        plan['limit'] = 5
    if (count and count.group(2) == '곳') or '중복 없이' in text:
        plan['unique_regions'] = True


def _apply_environment_ranking(plan, text, remaining, conditions, bot):
    if conditions or not re.search(r'바람.*약한|풍속.*(?:낮은|약한)|파도.*낮은|수온.*높은', text):
        return
    if not re.search(r'지역|곳|중', text):
        return
    named_regions = bot._parse_regions(remaining) or []
    if named_regions:
        plan['filters']['regions'] = named_regions
    metric = '평균수온' if '수온' in text else ('평균파고' if '파도' in text else '평균풍속')
    plan.update(operation='ranking', metric=metric, group_by='지역', aggregation='average',
                sort='desc' if '높은' in text else 'asc', clarification_needed=False)
    count = re.search(r'(\d+)\s*(개|곳)', text)
    plan['limit'] = int(count.group(1)) if count else 1
    plan['all_ties'] = bool(re.search(r'모두|동률|동점', text))


def _apply_hazard_lookup(plan, text, bot):
    rough_conditions = re.search(r'위험|거친\s*(?:바다|파도)|파도.*(?:높은|거센)|바람.*(?:강한|센)', text)
    place_context = re.search(r'낚시|지역|곳|어디|해역', text)
    if not (rough_conditions and place_context):
        return
    filters = plan['filters']
    if not filters.get('dates'):
        filters['dates'] = [(bot.base_date + timedelta(days=1)).isoformat()]
    filters['fish'] = []
    filters['fish_types'] = []
    filters['forecast_indexes'] = []
    for key in ('fish', 'forecast_indexes'):
        plan.get('exclude_filters', {}).pop(key, None)
    plan.update(operation='detail',
                select=['예측일자', '지역', '시간', '평균풍속', '최대풍속', '평균파고', '최대파고'],
                group_by=None, metric=None, sort=None, limit=5, return_all=False,
                environment_lookup=False, good_fishing_recommendation=False,
                hazard_lookup=True, clarification_needed=False, clarification_question=None)


def _apply_fishing_outlook(plan, text, bot):
    requested = bool(re.search(r'낚시(?:하기|가)?\s*어때|낚시.*괜찮아|출조.*어때', text))
    if not requested or plan['operation'] == 'unsupported':
        return
    filters = plan['filters']
    if not filters.get('dates'):
        filters['dates'] = [(bot.base_date + timedelta(days=1)).isoformat()]
    filters['forecast_indexes'] = []
    plan.get('exclude_filters', {}).pop('forecast_indexes', None)
    plan.update(operation='detail', select=['예측일자', '지역', '시간', '어종', '예보지수'],
                group_by=None, metric=None, sort=None, limit=None, return_all=True,
                environment_lookup=False, good_fishing_recommendation=False,
                fishing_outlook_lookup=True)
    if not filters.get('regions') and not filters.get('areas'):
        plan.update(clarification_needed=True,
                    clarification_question='어느 지역의 낚시 여건이 궁금하신가요? 지역 이름을 알려주세요.')
    else:
        plan.update(clarification_needed=False, clarification_question=None)


def refine_plan(plan, text, bot):
    """Apply deterministic plan stages in an explicit, tested order."""
    remaining, excluded = _apply_scope_filters(plan, text, bot)
    conditions = _numeric_conditions(text)
    _apply_numeric_plan(plan, text, conditions)
    _apply_good_recommendation(plan, text, remaining, excluded, conditions, bot)
    _apply_environment_ranking(plan, text, remaining, conditions, bot)
    _apply_hazard_lookup(plan, text, bot)
    _apply_fishing_outlook(plan, text, bot)
    return plan
