import ast
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from test_approved_api_behavior import API, ROOT, AttrDict, FrappeError, api_namespace, extract_functions


class ActionContractTests(unittest.TestCase):
    def test_schema_accepts_every_runtime_action_and_purpose(self):
        module = ast.parse(API.read_text())
        constants = {}
        for node in module.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id in {"ACTION_TYPES", "ACTION_PURPOSES"}:
                    constants[node.targets[0].id] = ast.literal_eval(node.value)
        schema = json.loads((ROOT / 'real_estate_crm_customs/real_estate_crm_customs/doctype/lead_action_execution/lead_action_execution.json').read_text())
        fields = {field['fieldname']: field for field in schema['fields']}
        self.assertTrue(set(constants['ACTION_TYPES']).issubset(set(fields['action_type']['options'].splitlines())))
        self.assertTrue(set(constants['ACTION_PURPOSES']).issubset(set(fields['purpose']['options'].splitlines())))
        self.assertIn('Closing', fields['action_type']['options'].splitlines())
        self.assertIn('Contract Signing', fields['purpose']['options'].splitlines())

    def namespace(self):
        clock = dt.datetime(2026, 10, 20, 12)
        namespace = api_namespace(get_datetime=dt.datetime.fromisoformat, now_datetime=lambda: clock,
            _next_action_is_required=lambda *_args: False,
            _standalone_interests_available=lambda: False,
            _validate_action_type=lambda *_args: None,
            _allowed_action_definitions=lambda *_args: [{'action_type': 'Call', 'purpose': 'General Follow-up'}],
            _validate_action_interest_scope=lambda *_args: None)
        extract_functions(API, {'_future_scheduled_time', '_validate_next_action_payload'}, namespace)
        return namespace, clock

    def test_successor_schedule_rejects_past_now_and_malformed_times(self):
        namespace, clock = self.namespace()
        validate = namespace['_validate_next_action_payload']
        for start in ('2026-10-19 12:00:00', str(clock), 'soon'):
            with self.subTest(start=start):
                with self.assertRaises(FrappeError):
                    validate(AttrDict(name='L'), AttrDict(), {'next_action': {
                        'action_type': 'Call', 'purpose': 'General Follow-up', 'scheduled_start': start, 'execute_now': 0}})
        result = {'next_action': {'action_type': 'Call', 'purpose': 'General Follow-up',
            'scheduled_start': '2026-10-21 12:00:00', 'execute_now': 0}}
        validate(AttrDict(name='L'), AttrDict(), result)
        self.assertEqual(result['next_action']['scheduled_start'], '2026-10-21 12:00:00')

    def test_immediate_successor_uses_server_time_without_client_date(self):
        namespace, clock = self.namespace()
        result = {'next_action': {'action_type': 'Call', 'purpose': 'General Follow-up', 'execute_now': 1}}
        namespace['_validate_next_action_payload'](AttrDict(name='L'), AttrDict(), result)
        self.assertEqual(result['next_action']['scheduled_start'], str(clock))

    def test_owner_alias_pending_repair_is_reachable_and_preserves_completed_history(self):
        patch = ROOT / 'real_estate_crm_customs/patches/v16_0/closing_requirements_and_meetings.py'
        rows = [AttrDict(name='valid', purpose='Meeting with Owner', workflow_status='Planned'),
            AttrDict(name='ambiguous', purpose='Meeting with Owner', workflow_status='Due'),
            AttrDict(name='history', purpose='General Meeting', workflow_status='Completed')]
        writes, reviewed = [], []
        db = SimpleNamespace(savepoint=lambda *_args: None, release_savepoint=lambda *_args: None,
            rollback=lambda **_kwargs: None, set_value=lambda *args, **kwargs: writes.append((args, kwargs)))
        namespace = {'frappe': SimpleNamespace(db=db, get_all=lambda *_args, **_kwargs: rows),
            '_has_columns': lambda *_args: True,
            '_pending_resale_owner_scope_is_valid': lambda action: action.name == 'valid',
            '_mark_meeting_for_review': lambda action: reviewed.append(action.name),
            '_log_issue': lambda *_args: None}
        extract_functions(patch, {'normalize_legacy_meetings'}, namespace)
        namespace['normalize_legacy_meetings']()
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][0][1], 'valid')
        self.assertEqual(reviewed, ['ambiguous'])
        self.assertNotIn('history', reviewed)
