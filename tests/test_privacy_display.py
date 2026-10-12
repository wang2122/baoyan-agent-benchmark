import unittest
from unittest.mock import patch
from scripts.privacy_display import scrub, scrub_value


class PrivacyDisplayTests(unittest.TestCase):
    def setUp(self):
        self.policy = {'common': {}, 'tasks': {'TASK': {
            'literals': {}, 'rankings': {'2/100': '前约2%'},
            'language_scores': {'547': '500–549'},
            'undergraduate_schools': {'示例大学': '一所211高校'}}}}

    def test_student_score_and_rank_are_generalized(self):
        with patch('scripts.privacy_display.rules', return_value=self.policy):
            self.assertEqual(scrub('排名2/100，六级547分', 'TASK'), '排名前约2%，六级500–549分')

    def test_official_requirement_and_target_school_are_preserved(self):
        with patch('scripts.privacy_display.rules', return_value=self.policy):
            self.assertEqual(scrub('要求六级547分；推荐示例大学的项目。', 'TASK'), '要求六级547分；推荐示例大学的项目。')
            self.assertEqual(scrub('本科院校：示例大学', 'TASK'), '本科院校：一所211高校')

    def test_personal_scores_under_threshold_heading_and_html(self):
        with patch('scripts.privacy_display.rules', return_value=self.policy):
            self.assertEqual(scrub('用硬门槛过滤：六级547过多数线。', 'TASK'), '用硬门槛过滤：六级500–549过多数线。')
            self.assertEqual(scrub('劣势<br>**六级547分**', 'TASK'), '劣势<br>**六级500–549分**')
            self.assertEqual(scrub('申请资格为四级547或六级425。', 'TASK'), '申请资格为四级547或六级425。')

    def test_derived_rank_percentage_and_institution_rate_are_distinct(self):
        self.policy['tasks']['TASK']['rankings']['1.92%']='约2%'
        with patch('scripts.privacy_display.rules', return_value=self.policy):
            self.assertEqual(scrub('Top 1.92%；学校推免率1.92%', 'TASK'), 'Top 约2%；学校推免率1.92%')

    def test_numeric_scores_and_real_run_fields_are_immutable(self):
        value = {'task_id': 'TASK', 'score': 79.08, 'mean_score_interval': [79.08, 79.08],
                 'model': 'model-547', 'started_at': '2026-10-11T12:00:00+00:00',
                 'answer_sha256': '547abcdef', 'reason': '六级547分'}
        with patch('scripts.privacy_display.rules', return_value=self.policy):
            result = scrub_value(value)
        for key in value:
            if key != 'reason': self.assertEqual(result[key], value[key])
        self.assertEqual(result['reason'], '六级500–549分')

    def test_display_transform_is_idempotent(self):
        with patch('scripts.privacy_display.rules', return_value=self.policy):
            text = scrub('排名2/100，六级547分', 'TASK')
            self.assertEqual(scrub(text, 'TASK'), text)


if __name__ == '__main__': unittest.main()
