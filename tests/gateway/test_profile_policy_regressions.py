"""Complementary controls for policy initialization and deferred tool callers."""

import json
import pytest
import yaml

import model_tools
import run_agent
from agent import agent_runtime_helpers
from tools.profile_policy import active_tool_policy, get_profile_tool_policy
from tools.registry import registry


def _configure(tmp_path, monkeypatch, extra=None):
    config = {
        'model': {'default': 'test/offline-model', 'context_length': 131072},
        'agent': {'environment_probe': False},
        'memory': {'memory_enabled': False, 'user_profile_enabled': False, 'provider': ''},
        'tools': {'tool_search': {'enabled': 'on'}},
    }
    config.update(extra or {})
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump(config), encoding='utf-8')
    model_tools._tool_defs_cache.clear()


def _offline_agent(**kwargs):
    run_agent._openrouter_prewarm_done.set()
    return run_agent.AIAgent(
        model='test/offline-model', provider='custom', api_key='test-offline-key',
        base_url='http://127.0.0.1:9/v1', quiet_mode=True, skip_context_files=True,
        **kwargs,
    )


def test_agent_rejects_missing_release_root_before_runtime_setup(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch, {
        'skills': {'external_only': True, 'external_dirs': [str(tmp_path / 'missing')]},
    })
    with pytest.raises(ValueError, match='usable skills.external_dirs'):
        _offline_agent(enabled_toolsets=['skills'])


def test_schema_cache_intersects_each_active_callable_ceiling(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    policy = get_profile_tool_policy()

    def names():
        return {d['function']['name'] for d in model_tools.get_tool_definitions(
            enabled_toolsets=['skills'], quiet_mode=True, skip_tool_search_assembly=True,
        )}

    assert 'skill_manage' in names()
    with active_tool_policy(policy, ['skills_list']):
        assert names() == {'skills_list'}
        with active_tool_policy(policy, ['skill_view']):
            assert names() == set()
        assert names() == {'skills_list'}
    assert 'skill_manage' in names()


def test_tool_search_preserves_authorized_deferred_calls_and_freezes_catalog(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    calls = []

    def register(name):
        registry.register(
            name=name, toolset='policy_regression',
            schema={'name': name, 'description': 'Read a disposable fixture.',
                    'parameters': {'type': 'object', 'properties': {}}},
            handler=lambda args, **kwargs: calls.append(name) or json.dumps({'success': True}),
        )

    register('policy_fixture_read')
    try:
        agent = _offline_agent(enabled_toolsets=['policy_regression'])
        assert 'tool_call' in {t['function']['name'] for t in agent.tools}
        assert 'policy_fixture_read' not in {t['function']['name'] for t in agent.tools}
        assert 'policy_fixture_read' in agent.valid_tool_names

        result = agent_runtime_helpers.invoke_tool(
            agent, 'policy_fixture_read', {}, 'policy-regression-turn',
        )
        assert json.loads(result)['success'] is True
        with active_tool_policy(agent._tool_policy, agent.valid_tool_names):
            result = model_tools.handle_function_call(
                'tool_call', {'name': 'policy_fixture_read', 'arguments': {}},
                enabled_tools=list(agent.valid_tool_names), enabled_toolsets=['policy_regression'],
            )
            assert json.loads(result)['success'] is True

        # A plugin reconnect must not widen an existing conversation's catalog.
        register('policy_fixture_late_read')
        with active_tool_policy(agent._tool_policy, agent.valid_tool_names):
            assert 'error' in json.loads(registry.dispatch('policy_fixture_late_read', {}))
        assert calls == ['policy_fixture_read', 'policy_fixture_read']
    finally:
        registry.deregister('policy_fixture_read')
        registry.deregister('policy_fixture_late_read')


def test_synthetic_bridge_schemas_obey_profile_denial(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch, {
        'agent': {'tool_policy': {'version': 1, 'denied_tools': ['tool_call']}},
    })
    registry.register(
        name='policy_bridge_fixture', toolset='policy_bridge_regression',
        schema={'name': 'policy_bridge_fixture', 'description': 'Read fixture.',
                'parameters': {'type': 'object', 'properties': {}}},
        handler=lambda args, **kwargs: '{}',
    )
    try:
        names = {d['function']['name'] for d in model_tools.get_tool_definitions(
            enabled_toolsets=['policy_bridge_regression'], quiet_mode=True,
        )}
        assert 'tool_search' in names
        assert 'tool_call' not in names
    finally:
        registry.deregister('policy_bridge_fixture')
