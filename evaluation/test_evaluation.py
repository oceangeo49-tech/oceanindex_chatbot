import copy
import unittest
from evaluation.run import check, source_rows
from evaluation.scenarios import build
from evaluation.chaos_run import case_list, check as chaos_check, normalize_expectation
from evaluation.qa_run import deterministic_issues

class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows=source_rows()
    def test_catalog_has_unique_ids_and_hundreds_of_questions(self):
        cases=build(self.rows)
        self.assertEqual(len(cases),len({c['id'] for c in cases}))
        self.assertGreater(sum(len(c['turns']) for c in cases),300)
        self.assertGreaterEqual(len({c['category'] for c in cases}),15)

    def test_chaos_generator_accepts_single_and_wrapped_cases(self):
        case = {'category': 'context_confusion', 'turns': [{'question': '질문'}]}
        self.assertEqual(case_list(case), [case])
        self.assertEqual(case_list({'cases': [case]}), [case])
        self.assertEqual(case_list([case]), [case])

    def test_chaos_unsupported_answer_is_not_failed_for_internal_date_context(self):
        response = {
            'reply': '지원하지 않는 어종입니다.',
            'result': {'kind': 'unsupported'},
            'context': {'regions': [], 'dates': ['2025-06-16'], 'pending': False},
        }
        expected = {'expect_kinds': ['unsupported'], 'no_items': True, 'context_action': 'reset'}
        self.assertEqual(chaos_check(response, expected, {'dates': ['2025-06-17']}), [])

    def test_chaos_corrects_impossible_region_introduction_label(self):
        kinds, no_items = normalize_expectation(
            '해양 관광지 중에서 가족과 함께 즐길 수 있는 곳을 추천해줘.',
            'irrelevant', ['region_introduction'], False,
        )
        self.assertEqual(kinds, ['unsupported', 'clarification'])
        self.assertTrue(no_items)

    def test_human_qa_detects_false_unknown_fish_reason(self):
        response = {
            'reply': "요청하신 어종 '예보는'은 현재 제공되는 어종 목록에 없습니다.",
            'result': {'kind': 'unsupported'},
        }
        issues = deterministic_issues('해양 예보는 낚시에만 관련된 것인가요?', response)
        self.assertTrue(any('어종을 묻지 않은 질문' in issue for issue in issues))
    def test_wrong_region_cannot_pass_with_correct_plan(self):
        expected={'kind':'detail','regions':['포항'],'dates':['2025-06-17'],'times':['AM'],'fields':['평균수온']}
        response={'plan':{'filters':{'regions':['포항'],'dates':['2025-06-17'],'times':['AM']}},
                  'result':{'kind':'detail','items':[{'지역':'방포항','예측일자':'2025-06-17','시간':'AM','평균수온':'20.2'}]},
                  'reply':'방포항의 평균수온은 20.2℃입니다.'}
        codes={e['code'] for e in check(response,expected,self.rows)}
        self.assertIn('data_scope',codes)
        self.assertIn('answer_scope',codes)
    def test_invented_number_does_not_pass(self):
        expected={'kind':'detail','regions':['가거도'],'dates':['2025-06-17'],'times':['PM'],'fields':['평균풍속']}
        response={'plan':{'filters':{'regions':['가거도'],'dates':['2025-06-17'],'times':['PM']}},
                  'result':{'kind':'detail','items':[{'지역':'가거도','예측일자':'2025-06-17','시간':'PM','평균풍속':'999'}]},
                  'reply':'가거도 풍속 999m/s'}
        self.assertIn('data_scope',{e['code'] for e in check(response,expected,self.rows)})
    def test_correct_csv_value_passes_but_added_measurement_fails(self):
        expected={'kind':'detail','regions':['가거도'],'dates':['2025-06-17'],
                  'times':['PM'],'fields':['평균풍속']}
        response={'plan':{'filters':{'regions':['가거도'],'dates':['2025-06-17'],'times':['PM']}},
                  'result':{'kind':'detail','items':[{'지역':'가거도','예측일자':'2025-06-17','시간':'PM','평균풍속':'3.4'}]},
                  'reply':'2025-06-17 오후 가거도의 평균 풍속은 3.4m/s입니다.'}
        self.assertEqual(check(response,expected,self.rows),[])
        response['reply']+=' 최대 999m/s입니다.'
        self.assertIn('answer_grounding',{e['code'] for e in check(response,expected,self.rows)})

    def test_empty_answer_does_not_pass_clarification(self):
        response={'result':{'kind':'clarification'},'reply':''}
        self.assertIn('empty_reply',{e['code'] for e in check(response,{'kind':'clarification'},self.rows)})
    def test_correct_result_passes_but_model_failure_is_reported(self):
        response={'result':{'kind':'clarification'},'reply':'어느 지역인가요?'}
        self.assertEqual(check(response,{'kind':'clarification'},self.rows),[])
        response['llm_error']='offline'
        self.assertIn('model_error',{e['code'] for e in check(response,{'kind':'clarification'},self.rows)})
    def test_duplicate_places_are_rejected(self):
        response={'reply':'추천합니다','plan':{'filters':{}},'result':{'kind':'detail','items':[{'지역':'가거도'},{'지역':'가거도'}]}}
        codes={f['code'] for f in check(response,{'kind':'detail','unique_regions':True,'max_items':2},self.rows)}
        self.assertIn('duplicate_region', codes)

    def test_numeric_operator_cannot_silently_change(self):
        response={'reply':'없습니다','plan':{'numeric_conditions':[{'field':'평균풍속','operator':'<=','value':5}]},'result':{'kind':'no_match'}}
        codes={f['code'] for f in check(response,{'kind':'no_match','conditions':[{'field':'평균풍속','operator':'<','value':5}]},self.rows)}
        self.assertIn('numeric_filter',codes)

if __name__=='__main__':unittest.main()
