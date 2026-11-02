#!/usr/bin/env python3
import unittest

from ocean_chatbot_app import (
    DATA_FILE,
    GUIDE_FILE,
    GenericCSVQueryEngine,
    HybridOceanChatbot,
    LLMQueryPlanner,
    OceanChatbot,
    OfficialGuide,
)


def make_plan(**updates):
    plan = {
        "operation": "detail",
        "filters": {
            "dates": [],
            "times": [],
            "areas": [],
            "regions": [],
            "fish": [],
            "fish_types": [],
            "tides": [],
            "forecast_indexes": [],
        },
        "numeric_conditions": [],
        "select": [],
        "group_by": None,
        "metric": None,
        "aggregation": "average",
        "sort": None,
        "limit": 5,
        "return_all": False,
        "include_guide": False,
        "environment_lookup": False,
        "term": None,
        "clarification_needed": False,
        "clarification_question": None,
        "unsupported_reason": None,
    }
    plan.update(updates)
    return plan


class GenericCSVQueryEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = OceanChatbot(DATA_FILE)
        cls.guide = OfficialGuide(GUIDE_FILE)
        cls.engine = GenericCSVQueryEngine(cls.bot, cls.guide)

    def test_all_red_seabream_service_regions(self):
        filters = make_plan()["filters"]
        filters["fish"] = ["참돔"]
        plan = make_plan(operation="distinct", filters=filters, select=["지역"], limit=None, return_all=True)
        result = self.engine.execute(plan)

        self.assertEqual(result["total"], 17)
        self.assertIn({"지역": "김녕"}, result["items"])
        self.assertIn({"지역": "거제도"}, result["items"])

    def test_lowest_wave_region_for_red_seabream_tomorrow(self):
        filters = make_plan()["filters"]
        filters["dates"] = ["2025-06-17"]
        filters["fish"] = ["참돔"]
        plan = make_plan(
            operation="ranking",
            filters=filters,
            group_by="지역",
            metric="평균파고",
            aggregation="average",
            sort="asc",
            limit=1,
        )
        result = self.engine.execute(plan)

        self.assertEqual(result["items"][0]["group"], "김녕")
        self.assertEqual(result["items"][0]["value"], 0.3)

    def test_very_good_region_fish_pairs_for_tomorrow_and_day_after(self):
        filters = make_plan()["filters"]
        filters["dates"] = ["2025-06-17", "2025-06-18"]
        filters["forecast_indexes"] = ["매우좋음"]
        plan = make_plan(
            operation="distinct",
            filters=filters,
            select=["예측일자", "지역", "어종"],
            limit=None,
            return_all=True,
        )
        result = self.engine.execute(plan)

        self.assertEqual(result["total"], 11)
        self.assertEqual(result["dates_without_results"], ["2025-06-17"])
        self.assertIn({"예측일자": "2025-06-18", "지역": "김녕", "어종": "참돔"}, result["items"])

    def test_all_forecast_indexes_for_ulsan_tomorrow(self):
        filters = make_plan()["filters"]
        filters["dates"] = ["2025-06-17"]
        filters["regions"] = ["울산"]
        plan = make_plan(
            operation="detail",
            filters=filters,
            select=["예측일자", "지역", "시간", "어종", "총점수", "예보지수"],
            limit=None,
            return_all=True,
        )
        result = self.engine.execute(plan)

        self.assertEqual(result["total"], 14)
        self.assertIn(
            {
                "예측일자": "2025-06-17",
                "지역": "울산",
                "시간": "PM",
                "어종": "우럭",
                "총점수": "47.97",
                "예보지수": "매우나쁨",
            },
            result["items"],
        )

    def test_detail_can_include_official_guide_context(self):
        filters = make_plan()["filters"]
        filters["dates"] = ["2025-06-17"]
        filters["regions"] = ["울산"]
        filters["fish"] = ["참돔"]
        plan = make_plan(
            operation="detail",
            filters=filters,
            select=["예측일자", "지역", "시간", "어종", "총점수", "예보지수"],
            limit=None,
            return_all=True,
            include_guide=True,
        )
        result = self.engine.execute(plan)

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["guide"]["index_levels"][0]["term"], "나쁨")
        self.assertIn("경향", result["guide"]["fish"][0]["definition"])

    def test_environment_detail_removes_duplicate_fish_rows(self):
        filters = make_plan()["filters"]
        filters["dates"] = ["2025-06-17"]
        filters["regions"] = ["울릉도"]
        plan = make_plan(
            operation="detail",
            filters=filters,
            select=["예측일자", "지역", "시간", "최저기온", "평균기온", "최대기온"],
            limit=None,
            return_all=True,
            environment_lookup=True,
        )
        result = self.engine.execute(plan)

        self.assertEqual(result["total"], 2)
        self.assertNotIn("어종", result["items"][0])
        self.assertEqual(result["items"][0]["평균기온"], "23.3")
        self.assertNotIn("data_warnings", result)

    def test_official_guide_uses_cautious_fish_wording(self):
        result = self.engine.execute(make_plan(operation="explain", term="참돔", include_guide=True))

        self.assertEqual(result["category"], "fish")
        self.assertIn("경향", result["definition"])
        self.assertIn("상대적으로 유리할 수 있습니다", result["definition"])


class QueryPlanValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = OceanChatbot(DATA_FILE)
        cls.planner = LLMQueryPlanner(None, cls.bot)

    def test_sea_names_expand_to_csv_areas_even_if_model_drops_them(self):
        cases = [('동해', ['동해남부', '동해중부']),
                 ('동해중부', ['동해중부']),
                 ('서해', ['황해남부', '황해중부']),
                 ('남해', ['남해동부', '남해서부']),
                 ('동해중부와 남해', ['남해동부', '남해서부', '동해중부'])]
        for phrase, expected in cases:
            raw = {'operation': 'detail', 'filters': {'dates': ['2025-06-17'],
                   'areas': ['황해남부'], 'regions': ['하조도']}}
            plan = self.planner.validate(raw, f'내일 {phrase}에서 낚시하기 좋은 지역 알려줘')
            self.assertEqual(plan['filters']['areas'], expected)
            self.assertEqual(plan['filters']['regions'], [])
            self.assertEqual(plan['filters']['forecast_indexes'], ['좋음', '매우좋음'])
            engine = GenericCSVQueryEngine(self.bot)
            rows = engine.filter_rows(plan)
            self.assertTrue(rows)
            self.assertTrue(all(row['권역'] in expected for row in rows))
            self.assertTrue(all(row['예보지수'] in ['좋음', '매우좋음'] for row in rows))
            self.assertLessEqual(len(engine.execute(plan)['items']), 5)

    def test_removes_hallucinated_threshold_and_wrong_area_values(self):
        raw = {
            "operation": "aggregate",
            "filters": {"dates": ["2025-06-17"], "areas": cls_regions(self.bot), "fish": ["참돔"]},
            "numeric_conditions": [{"field": "평균파고", "operator": "<=", "value": 1.0}],
            "select": ["지역"],
            "group_by": "지역",
            "metric": "평균파고",
            "aggregation": "average",
            "sort": "asc",
            "limit": 5,
        }
        plan = self.planner.validate(raw, "내일 참돔 지역 중 파도가 가장 낮은 곳")

        self.assertEqual(plan["operation"], "ranking")
        self.assertEqual(plan["filters"]["areas"], [])
        self.assertEqual(plan["numeric_conditions"], [])
        self.assertEqual(plan["limit"], 1)

    def test_forecast_index_lookup_is_not_treated_as_definition(self):
        raw = {
            "operation": "explain",
            "filters": {"dates": ["2025-06-17"], "regions": ["울산"]},
            "term": "예보지수",
        }
        plan = self.planner.validate(raw, "내일 울산 예보지수 알려줘")

        self.assertEqual(plan["operation"], "detail")
        self.assertTrue(plan["return_all"])
        self.assertIsNone(plan["limit"])
        self.assertEqual(plan["select"], ["예측일자", "지역", "시간", "어종", "총점수", "예보지수"])

    def test_fishing_index_alias_uses_forecast_index_detail(self):
        raw = {
            "operation": "detail",
            "filters": {"dates": ["2025-06-17"], "regions": ["울산"]},
            "select": ["지역", "어종"],
        }
        plan = self.planner.validate(raw, "내일 울산 바다낚시지수 알려줘")

        self.assertEqual(plan["operation"], "detail")
        self.assertTrue(plan["return_all"])
        self.assertIn("예보지수", plan["select"])

    def test_forecast_index_definition_stays_explain(self):
        raw = {"operation": "detail", "filters": {}, "select": ["예보지수"]}
        plan = self.planner.validate(raw, "예보지수가 뭐야?")

        self.assertEqual(plan["operation"], "explain")
        self.assertEqual(plan["term"], "예보지수")

    def test_fish_characteristics_use_official_guide(self):
        raw = {"operation": "detail", "filters": {"fish": ["참돔"]}}
        plan = self.planner.validate(raw, "참돔 특징과 서식 환경을 알려줘")

        self.assertEqual(plan["operation"], "explain")
        self.assertEqual(plan["term"], "참돔")
        self.assertTrue(plan["include_guide"])

    def test_index_level_meaning_uses_official_guide(self):
        raw = {"operation": "detail", "filters": {"forecast_indexes": ["매우좋음"]}}
        plan = self.planner.validate(raw, "매우좋음은 어떤 단계야?")

        self.assertEqual(plan["operation"], "explain")
        self.assertEqual(plan["term"], "매우좋음")

    def test_index_value_and_meaning_keep_detail_and_add_guide(self):
        raw = {
            "operation": "explain",
            "filters": {"dates": ["2025-06-17"], "regions": ["울산"], "fish": ["참돔"]},
            "term": "예보지수",
        }
        plan = self.planner.validate(raw, "내일 울산 참돔 지수와 의미를 알려줘")

        self.assertEqual(plan["operation"], "detail")
        self.assertTrue(plan["include_guide"])
        self.assertTrue(plan["return_all"])

    def test_explicit_environment_region_overrides_extra_or_missing_model_regions(self):
        for regions in [['가거도', '거문도'], [], ['거문도']]:
            raw = {'operation': 'detail', 'filters': {'dates': ['2025-06-17'],
                   'times': ['PM'], 'regions': regions}, 'select': ['평균풍속']}
            plan = self.planner.validate(raw, '“내일 오후 가거도 풍속을 알려줘”')
            self.assertEqual(plan['filters']['regions'], ['가거도'])
            result = GenericCSVQueryEngine(self.bot).execute(plan)
            self.assertTrue(result['items'])
            self.assertEqual({item['지역'] for item in result['items']}, {'가거도'})

    def test_explicit_multiple_environment_regions_are_preserved(self):
        raw = {'operation': 'detail', 'filters': {'dates': ['2025-06-17'], 'regions': ['가거도']}}
        plan = self.planner.validate(raw, '내일 가거도와 거문도 풍속 알려줘')
        self.assertEqual(set(plan['filters']['regions']), {'가거도', '거문도'})

    def test_temperature_lookup_is_normalized_to_environment_detail(self):
        raw = {
            "operation": "detail",
            "filters": {"dates": ["2025-06-17"], "regions": ["울릉도"]},
            "select": ["예측일자", "지역", "시간", "어종", "평균기온"],
            "return_all": True,
        }
        plan = self.planner.validate(raw, "내일 울릉도 기온")

        self.assertTrue(plan["environment_lookup"])
        self.assertEqual(plan["filters"]["fish"], [])
        self.assertNotIn("어종", plan["select"])
        self.assertEqual(
            plan["select"],
            ["예측일자", "지역", "시간", "최저기온", "평균기온", "최대기온"],
        )
        self.assertTrue(plan["return_all"])


class EnvironmentAnswerTests(unittest.TestCase):
    def setUp(self):
        self.result = {
            "kind": "detail",
            "total": 2,
            "items": [
                {
                    "예측일자": "2025-06-17",
                    "지역": "울릉도",
                    "시간": "AM",
                    "최저기온": "23.1",
                    "평균기온": "23.3",
                    "최대기온": "23.4",
                },
                {
                    "예측일자": "2025-06-17",
                    "지역": "울릉도",
                    "시간": "PM",
                    "최저기온": "22.6",
                    "평균기온": "23.1",
                    "최대기온": "23.5",
                },
            ],
        }

    def test_grounded_environment_answer_is_accepted(self):
        reply = (
            "2025-06-17 울릉도의 평균기온은 오전 23.3℃, 오후 23.1℃입니다. "
            "예상 범위는 오전 23.1~23.4℃, 오후 22.6~23.5℃입니다."
        )

        self.assertTrue(HybridOceanChatbot._environment_answer_is_grounded(reply, self.result))

    def test_changed_or_missing_environment_value_is_rejected(self):
        reply = "2025-06-17 울릉도의 평균기온은 오전 24.0℃, 오후 23.1℃입니다."

        self.assertFalse(HybridOceanChatbot._environment_answer_is_grounded(reply, self.result))

    def test_mechanical_or_unsupported_environment_prose_is_rejected(self):
        reply = (
            "2025-06-17 울릉도 기온입니다.\n"
            "- AM: 최저 23.1℃, 평균 23.3℃, 최대 23.4℃\n"
            "- PM: 최저 22.6℃, 평균 23.1℃, 최대 23.5℃\n"
            "전반적으로 낮은 수준입니다."
        )

        self.assertFalse(HybridOceanChatbot._environment_answer_is_grounded(reply, self.result))

    def test_single_slot_environment_uses_natural_word_order(self):
        chatbot = HybridOceanChatbot.__new__(HybridOceanChatbot)
        result = {'items': [{'예측일자': '2025-06-17', '지역': '가거도', '시간': 'PM',
                  '최소풍속': '2.7', '평균풍속': '3.4', '최대풍속': '4.7'}]}
        reply = chatbot._environment_result_text(result)
        self.assertIn('2025-06-17 오후 가거도의 평균 풍속은 3.4m/s입니다.', reply)
        self.assertIn('예상 풍속 범위는 2.7~4.7m/s입니다.', reply)
        self.assertNotIn('평균풍속은 오후', reply)

    def test_environment_fallback_uses_natural_sentences(self):
        chatbot = HybridOceanChatbot.__new__(HybridOceanChatbot)
        reply = chatbot._environment_result_text(self.result)

        self.assertIn("평균기온은 오전 23.3℃, 오후 23.1℃입니다.", reply)
        self.assertIn("예상 범위는 오전 23.1~23.4℃, 오후 22.6~23.5℃입니다.", reply)
        self.assertNotIn("조건에 맞는 결과는 총", reply)
        self.assertNotIn("예측일자:", reply)


def cls_regions(bot):
    return list(bot.regions)




class GoodFishingRecommendationTests(unittest.TestCase):
    def setUp(self):
        self.bot = HybridOceanChatbot(DATA_FILE)
        self.raw = make_plan(return_all=True)
        self.raw['filters']['dates'] = ['2025-06-17']

    def query(self, question, raw=None):
        plan = self.bot.planner.validate(raw or self.raw, question)
        return plan, self.bot.query_engine.execute(plan)

    def test_unfiltered_model_plan_is_corrected(self):
        plan, result = self.query('내일 낚시하기 좋은지역')
        self.assertEqual(plan['filters']['dates'], ['2025-06-17'])
        self.assertEqual(plan['filters']['forecast_indexes'], ['좋음', '매우좋음'])
        self.assertEqual(result['total'], 44)
        self.assertEqual(len(result['items']), 5)
        self.assertTrue(all(item['예보지수'] == '좋음' for item in result['items']))
        scores = [float(item['총점수']) for item in result['items']]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all({'지역', '시간', '어종'} <= item.keys() for item in result['items']))

    def test_good_index_region_question_corrects_unfiltered_regional_ranking(self):
        raw = make_plan(operation='ranking', group_by='지역', metric='총점수', sort='desc')
        raw['filters']['dates'] = ['2025-06-17']
        plan, result = self.query('내일 지수가 좋은지역', raw)
        self.assertEqual(plan['filters']['forecast_indexes'], ['좋음', '매우좋음'])
        self.assertEqual(plan['operation'], 'detail')
        self.assertEqual(result['total'], 44)
        self.assertTrue(all(i['예보지수'] in ['좋음', '매우좋음'] for i in result['items']))
        reply = self.bot._fallback_result_text(result)
        self.assertNotIn('보통', reply)
        self.assertNotIn('나쁨', reply)

    def test_explicit_all_and_limit(self):
        _, result = self.query('내일 낚시하기 좋은 지역 전부 알려줘')
        self.assertEqual(len(result['items']), 44)
        self.assertEqual(len(self.query('내일 낚시하기 좋은 지역 3건')[1]['items']), 3)

    def test_existing_time_and_fish_conditions_are_kept(self):
        self.raw['filters'].update(times=['PM'], fish=['참돔'])
        plan, result = self.query('내일 오후 참돔 낚시하기 좋은 지역')
        self.assertEqual(plan['filters']['times'], ['PM'])
        self.assertTrue(all(i['시간'] == 'PM' and i['어종'] == '참돔' for i in result['items']))

    def test_no_good_match_does_not_relax_grade(self):
        plan, result = self.query('내일 낚시하기 매우 좋은 지역')
        self.assertEqual(plan['filters']['forecast_indexes'], ['매우좋음'])
        self.assertEqual(result['kind'], 'no_match')
        self.assertIn('자료가 없습니다', self.bot._fallback_result_text(result))

    def test_plain_index_lookup_and_definitions_not_restricted(self):
        plan, _ = self.query('내일 예보지수 알려줘')
        self.assertEqual(plan['filters']['forecast_indexes'], [])
        plan, _ = self.query('좋음 등급의 의미가 뭐야')
        self.assertEqual(plan['operation'], 'explain')
        self.assertFalse(plan['good_fishing_recommendation'])

    def test_offline_fallback_keeps_date_and_grade(self):
        from unittest.mock import patch
        with patch.object(self.bot.client, 'chat', side_effect=RuntimeError('offline')):
            reply = self.bot.answer('내일 낚시하기 좋은지역')
        self.assertEqual(reply['plan']['filters']['dates'], ['2025-06-17'])
        self.assertEqual(reply['result']['total'], 44)
        self.assertTrue(all(i['예보지수'] == '좋음' for i in reply['items']))
        self.assertIn('조합은 총 44건', reply['reply'])

    def test_recommendation_renderer_does_not_use_second_llm(self):
        from unittest.mock import patch
        with patch.object(self.bot.planner, 'parse', return_value=self.query('내일 낚시하기 좋은지역')[0]), patch.object(self.bot, '_natural_answer') as model:
            reply = self.bot.answer('내일 낚시하기 좋은지역')
        model.assert_not_called()
        self.assertIn('좋음·매우좋음만', reply['reply'])


    def test_offline_all_results_does_not_force_daily_time_slot(self):
        from unittest.mock import patch
        with patch.object(self.bot.client, 'chat', side_effect=RuntimeError('offline')):
            reply = self.bot.answer('내일 낚시하기 좋은 지역 전체 알려줘')
        self.assertEqual(reply['plan']['filters']['times'], [])
        self.assertEqual(len(reply['items']), 44)


class PublicPresentationTests(unittest.TestCase):
    def setUp(self):
        self.bot = HybridOceanChatbot(DATA_FILE)

    def test_explicit_corrections_keep_other_slots_and_reset_discards_them(self):
        first = self.bot.answer('내일 오후 가거도 수온')
        for text, region, day, topic in [
            ('아니 하조도로 바꿔줘', '하조도', '2025-06-17', '수온'),
            ('수온 말고 풍속', '가거도', '2025-06-17', '풍속'),
            ('내일 말고 모레', '가거도', '2025-06-18', '수온')]:
            answer = self.bot.answer(text, first['context'])
            self.assertEqual(answer['context']['regions'], [region])
            self.assertEqual(answer['context']['dates'], [day])
            self.assertEqual(answer['context']['times'], ['PM'])
            self.assertEqual(answer['context']['topic'], topic)
            self.assertTrue(answer['items'])
        reset = self.bot.answer('다른 질문할게', first['context'])
        answer = self.bot.answer('풍속', reset['context'])
        self.assertEqual(answer['result']['kind'], 'clarification')
        self.assertEqual(answer['context']['regions'], [])

    def test_unsupported_recommendation_is_not_converted_to_fishing(self):
        plan = self.bot.planner.validate({'operation':'unsupported', 'filters':{}, 'unsupported_reason':'주식은 자료에 없습니다.'}, '오늘 주식 추천해줘')
        self.assertEqual(plan['operation'], 'unsupported')
        self.assertFalse(plan.get('good_fishing_recommendation'))

    def test_unknown_species_recommendation_is_not_silently_dropped(self):
        plan = self.bot.planner.validate({'operation':'detail', 'filters':{}}, '내일 호랑이 잡기 좋은 지역 알려줘')
        self.assertEqual(plan['operation'], 'unsupported')
        self.assertFalse(plan.get('good_fishing_recommendation'))
        result = self.bot.query_engine.execute(plan)
        self.assertEqual(result['kind'], 'unsupported')
        self.assertIn('호랑이', result['reason'])

    def test_generic_fishing_words_are_not_treated_as_unknown_species(self):
        for question in [
            '해양 낚시 하기 좋은 곳 알려줘',
            '그곳에서 낚시는 가능한가요?',
            '그 지역에선 낚시 못해?',
            '어디에 낚시 하면 잘 잡아?',
        ]:
            plan = self.bot.planner.validate({'operation': 'clarification', 'filters': {}}, question)
            self.assertNotIn('요청하신 어종', plan.get('unsupported_reason') or '')

    def test_unrelated_good_place_request_is_not_treated_as_fishing(self):
        plan = self.bot.planner.validate({'operation':'detail', 'filters':{}}, '내일 싸움하기 좋은 지역 알려줘')
        self.assertEqual(plan['operation'], 'unsupported')
        self.assertFalse(plan.get('good_fishing_recommendation'))

    def test_tourism_recommendation_is_not_treated_as_fishing(self):
        answer = self.bot.answer('해양 관광지 중에서 가족과 함께 즐길 수 있는 곳을 추천해줘')
        self.assertEqual(answer['result']['kind'], 'unsupported')
        self.assertNotIn('어종 조합', answer['reply'])
        self.assertNotIn('지수 좋음', answer['reply'])

    def test_broad_fishing_recommendation_without_date_asks_for_date(self):
        for question in [
            '해양 낚시 하기 좋은 곳 알려줘',
            '해양 낚시할 만한 장소 있나?',
            '해양 낚시를 할 만한 지역은 어디인가요?',
        ]:
            answer = self.bot.answer(question)
            self.assertEqual(answer['result']['kind'], 'clarification')
            self.assertIn('날짜', answer['reply'])
            self.assertNotIn('요청하신 어종', answer['reply'])

    def test_fishing_pronoun_without_context_requests_region(self):
        for question in ['그곳에서 낚시는 가능한가요?', '그 지역에선 낚시 못해?']:
            answer = self.bot.answer(question)
            self.assertEqual(answer['result']['kind'], 'clarification')
            self.assertIn('지역 이름', answer['reply'])

    def test_poor_index_does_not_mean_fishing_is_impossible(self):
        answer = self.bot.answer('해양 지역에서 조황이 안 좋은 날에는 어디서도 낚시가 안 되는가?')
        self.assertEqual(answer['result']['kind'], 'explain')
        self.assertIn('불가능하다는 뜻은 아닙니다', answer['reply'])
        self.assertNotIn('조건에 맞는 결과', answer['reply'])

    def test_pre_trip_and_best_weather_questions_use_cautious_guidance(self):
        preparation = self.bot.answer('해양 낚시를 하기 전에 반드시 확인해야 할 사항이 있을까?')
        self.assertEqual(preparation['result']['kind'], 'explain')
        self.assertIn('해상특보', preparation['reply'])
        self.assertNotIn('성공률', preparation['reply'])
        weather = self.bot.answer('해양 낚시를 할 때 어떤 날씨가 가장 좋나요?')
        self.assertEqual(weather['result']['kind'], 'explain')
        self.assertIn('단정할 수는 없습니다', weather['reply'])

    def test_weekend_and_invalid_dates_use_real_calendar(self):
        from ocean_query_policy import dates_in
        self.assertEqual(dates_in('이번 주말', self.bot.base_date), ['2025-06-21', '2025-06-22'])
        answer = self.bot.answer('2025-02-30 하조도 수온')
        self.assertEqual(answer['result']['kind'], 'clarification')
        self.assertIn('존재하지', answer['reply'])

    def test_comparison_keeps_explicit_regions_even_if_model_drops_them(self):
        plan = self.bot.planner.validate({'operation':'ranking','filters':{}}, '내일 가거도와 하조도 중 오후 바람이 더 약한 곳은?')
        self.assertEqual(set(plan['filters']['regions']), {'가거도','하조도'})

    def test_recommendation_hides_scores_but_keeps_internal_ranking(self):
        from unittest.mock import patch
        with patch.object(self.bot.client, 'chat', side_effect=RuntimeError('offline')):
            result = self.bot.answer('내일 지수가 좋은지역')
        reply = result['reply']
        for hidden in ['예보지수', '총점수', '기타어종', '90.37', '89.21', 'PM', 'AM']:
            self.assertNotIn(hidden, reply)
        for visible in ['지수: 좋음', '어종: 기타', '오후', '2025-06-17']:
            self.assertIn(visible, reply)
        self.assertEqual(result['items'][0]['총점수'], '90.37')
        self.assertEqual(result['items'][0]['어종'], '기타어종')

    def test_grouped_ranking_does_not_display_average_or_representative_scores(self):
        result = {'kind':'ranking','total':1,'items':[{'group':'영흥도','metric':'총점수',
            'value':84.65,'대표예보지수':'좋음','대표총점수':'89.21','대표어종':'기타어종','대표시간':'PM'}]}
        reply = self.bot._public_text(self.bot._fallback_result_text(result))
        for hidden in ['84.65','89.21','총점','예보지수','기타어종']:
            self.assertNotIn(hidden, reply)
        self.assertIn('지수: 좋음', reply)
        self.assertIn('어종: 기타', reply)

    def test_public_names_also_work_in_questions(self):
        from unittest.mock import patch
        with patch.object(self.bot, '_answer_internal', return_value={'reply':'예보지수 기타어종'}) as internal:
            result = self.bot.answer('내일 기타 낚시하기 좋은 지역')
        self.assertIn('기타어종', internal.call_args.args[0])
        self.assertEqual(result['reply'], '지수 기타')

    def test_public_answer_does_not_expose_csv_storage_format(self):
        reply = self.bot._public_text('CSV 데이터에는 필수 장비 정보가 포함되어 있지 않습니다.')
        self.assertEqual(reply, '데이터에는 필수 장비 정보가 포함되어 있지 않습니다.')

    def test_shared_date_is_shown_once_without_mutating_rows(self):
        import copy
        result = {'kind': 'detail', 'items': [
            {'예측일자': '2025-06-18', '지역': '김녕', '어종': fish, '예보지수': '매우좋음'}
            for fish in ['참돔', '기타어종']]}
        original = copy.deepcopy(result)
        reply = self.bot._public_text(self.bot._fallback_result_text(result))
        self.assertEqual(reply.count('2025-06-18'), 1)
        self.assertTrue(reply.startswith('조회 날짜: 2025-06-18'))
        self.assertEqual(result, original)
        self.assertIn('어종: 기타', reply)

    def test_mixed_dates_stay_with_each_row(self):
        result = {'kind': 'detail', 'items': [
            {'예측일자': day, '지역': '김녕'} for day in ['2025-06-17', '2025-06-18']]}
        reply = self.bot._public_text(self.bot._fallback_result_text(result))
        self.assertIn('1. 날짜: 2025-06-17', reply)
        self.assertIn('2. 날짜: 2025-06-18', reply)
        self.assertNotIn('조회 날짜:', reply)

    def test_top_one_is_not_a_display_limit(self):
        plan = make_plan(operation='ranking', group_by='지역', metric='평균풍속',
                         aggregation='average', sort='asc', limit=1)
        plan['filters']['dates'] = ['2025-06-17']
        plan['filters']['times'] = ['PM']
        result = self.bot.query_engine.execute(plan)
        self.assertGreater(result['total'], 1)
        self.assertEqual(len(result['items']), 1)
        self.assertFalse(result['display_limit_reached'])
        reply = self.bot._public_text(self.bot._fallback_result_text(result))
        self.assertNotIn('표시 한도', reply)
        self.assertNotIn('총 34개', reply)
        self.assertIn('m/s', reply)

    def test_full_list_cap_still_explains_omission(self):
        plan = make_plan(operation='detail', return_all=True, limit=None)
        result = self.bot.query_engine.execute(plan)
        self.assertTrue(result['display_limit_reached'])
        self.assertIn('표시 한도', self.bot._fallback_result_text(result))

    def test_region_only_gives_grounded_introduction_without_llm(self):
        from unittest.mock import patch
        with patch.object(self.bot.client, 'chat', side_effect=AssertionError('LLM not needed')):
            for question in ['하조도', '  하조도  ', '“하조도”', '하조도?']:
                reply = self.bot.answer(question)['reply']
                self.assertIn('하조도는 황해남부 권역', reply)
                self.assertIn('어떤 정보가 궁금하신가요?', reply)
                self.assertNotIn('가거도', reply)
                self.assertNotIn('총 34', reply)

    def test_complete_direct_lookup_uses_explicit_conditions(self):
        result = self.bot.answer('내일 하조도 풍속 알려줘')
        self.assertEqual(result['plan']['filters']['regions'], ['하조도'])
        self.assertEqual(result['plan']['filters']['dates'], ['2025-06-17'])
        self.assertIn('평균풍속', result['reply'])

    def test_bare_topics_request_missing_conditions(self):
        for topic in ['수온', '풍속', '바람', '파고', '파도', '기온', '유속', '바다낚시지수']:
            result = self.bot.answer(topic)
            self.assertEqual(result['result']['kind'], 'clarification')
            self.assertIn('어느 지역', result['reply'])
            self.assertIn('날짜', result['reply'])
            self.assertNotIn('items', result)

    def test_region_topic_date_conversation(self):
        intro = self.bot.answer('하조도')
        question = self.bot.answer('수온', intro['context'])
        self.assertIn('하조도의 수온', question['reply'])
        self.assertIn('언제', question['reply'])
        answer = self.bot.answer('내일 오후', question['context'])
        self.assertEqual(answer['plan']['filters']['regions'], ['하조도'])
        self.assertEqual(answer['plan']['filters']['dates'], ['2025-06-17'])
        self.assertEqual(answer['plan']['filters']['times'], ['PM'])
        self.assertIn('평균 수온', answer['reply'])
        self.assertNotIn('가거도', answer['reply'])

    def test_topic_date_region_conversation(self):
        first = self.bot.answer('수온')
        second = self.bot.answer('내일', first['context'])
        self.assertIn('어느 지역', second['reply'])
        third = self.bot.answer('가거도', second['context'])
        self.assertEqual(third['plan']['filters']['regions'], ['가거도'])
        self.assertFalse(third['context']['pending'])

    def test_conversations_do_not_share_context(self):
        first = self.bot.answer('하조도')
        self.bot.answer('수온', first['context'])
        separate = self.bot.answer('수온')
        self.assertEqual(separate['context']['regions'], [])
        self.assertIn('어느 지역', separate['reply'])

    def test_definition_and_ranking_do_not_inherit_narrow_scope(self):
        from unittest.mock import patch
        state = {'regions': ['하조도'], 'dates': ['2025-06-17'], 'topic': '수온', 'pending': True}
        self.assertEqual(self.bot.answer('수온이 뭐야?', state)['result']['kind'], 'explain')
        for text in ['내일 수온 가장 높은 지역', '전체 지역 수온 알려줘']:
            with patch.object(self.bot, '_answer_current', return_value={'reply': 'normal'}) as normal:
                self.assertEqual(self.bot.answer(text, state)['reply'], 'normal')
                normal.assert_called_once_with(text)

    def test_tomorrow_after_tomorrow_keeps_csv_date_basis(self):
        result = self.bot.answer('내일모레 하조도 수온')
        self.assertEqual(result['plan']['filters']['dates'], ['2025-06-18'])

    def test_explicit_calendar_date_returns_no_match_without_exception(self):
        result = self.bot.answer('2099-01-01 하조도 수온 알려줘')
        self.assertEqual(result['result']['kind'], 'no_match')
        self.assertEqual(result['plan']['filters']['dates'], ['2099-01-01'])

    def test_exact_region_and_multiple_names_do_not_use_substring_identity(self):
        for question, expected in [('내일 포항 수온', {'포항'}),
                                   ('내일 방포항 수온', {'방포항'}),
                                   ('내일 포항과 방포항 수온', {'포항', '방포항'})]:
            result = self.bot.answer(question)
            self.assertEqual({row['지역'] for row in result['items']}, expected)

    def test_unsupported_place_cannot_silently_expand_environment_query(self):
        from unittest.mock import patch
        import json
        raw = {'operation': 'detail', 'filters': {'dates': ['2025-06-17'], 'regions': []},
               'select': ['평균수온'], 'return_all': True}
        with patch.object(self.bot.client, 'chat', return_value=json.dumps(raw)):
            result = self.bot.answer('내일 아틀란티스 수온 알려줘')
        self.assertEqual(result['result']['kind'], 'clarification')
        self.assertFalse(result.get('items'))

    def test_conversation_does_not_become_an_environment_query(self):
        from unittest.mock import patch
        with patch.object(self.bot.client, 'chat', side_effect=AssertionError('No LLM required')):
            for question in ['안녕하세요', '고마워요', '내일 낚시하기 싫어', '오늘은 쉬고 싶어']:
                result = self.bot.answer(question)
                self.assertEqual(result['result']['kind'], 'conversation')
                self.assertFalse(result.get('items'))

    def test_wave_particle_is_natural(self):
        result = self.bot.answer('내일 오후 하조도 파고')
        self.assertIn('평균 파고는', result['reply'])
        self.assertNotIn('파고은', result['reply'])

    def test_ui_keeps_date_without_developer_panels(self):
        from ocean_chatbot_app import UI_FILE
        page = UI_FILE.read_text()
        for hidden in ['modelStatus','rowCount','metaBox','처리 구조','Qwen3','처리 경로:','예보지수란?']:
            self.assertNotIn(hidden, page)
        self.assertIn('baseDate', page)
        self.assertIn('지원하는 질문', page)


if __name__ == "__main__":
    unittest.main()
