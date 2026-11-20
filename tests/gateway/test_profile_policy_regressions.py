"""Complementary controls for policy initialization and deferred tool callers."""

import json
import os
import subprocess
import sys
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


@pytest.mark.parametrize('excluded_dir', ['.git', 'node_modules/pkg', '.venv'])
def test_legacy_skill_lookup_ignores_excluded_directories(tmp_path, monkeypatch, excluded_dir):
    from tools.skills_tool import skill_view

    release = tmp_path / 'release'
    released_skill = release / 'fixture'
    released_skill.mkdir(parents=True)
    (released_skill / 'SKILL.md').write_text('RELEASED_BODY', encoding='utf-8')
    hidden = release / excluded_dir
    hidden.mkdir(parents=True)
    (hidden / 'fixture.md').write_text('HIDDEN_BODY', encoding='utf-8')
    legacy = release / 'category'
    legacy.mkdir()
    (legacy / 'legacy.md').write_text('LEGACY_BODY', encoding='utf-8')
    _configure(tmp_path, monkeypatch, {
        'skills': {'external_only': True, 'external_dirs': [str(release)]},
    })

    result = json.loads(skill_view('fixture', preprocess=False))
    assert result['success'] is True
    assert 'RELEASED_BODY' in result['content']
    assert 'HIDDEN_BODY' not in result['content']
    assert 'LEGACY_BODY' in json.loads(skill_view('legacy', preprocess=False))['content']


@pytest.mark.parametrize('external_only', [True, False])
def test_slash_discovery_respects_external_only_profile(tmp_path, monkeypatch, external_only):
    from agent import skill_commands
    from tools import skills_tool

    release = tmp_path / 'release'
    local = tmp_path / 'skills'
    for root, name in [(release, 'released-fixture'), (local, 'local-fixture')]:
        skill = root / name
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text(
            f'---\nname: {name}\ndescription: Read fixture.\n---\nBODY\n', encoding='utf-8',
        )
    _configure(tmp_path, monkeypatch, {
        'skills': {'external_only': external_only, 'external_dirs': [str(release)]},
    })
    monkeypatch.setattr(skills_tool, 'SKILLS_DIR', local)
    monkeypatch.setattr(skill_commands, '_skill_commands', {})
    monkeypatch.setattr(skill_commands, '_skill_commands_platform', None)

    commands = skill_commands.scan_skill_commands()
    assert '/released-fixture' in commands
    assert ('/local-fixture' in commands) is (not external_only)
    assert commands['/released-fixture']['skill_dir'] == str(release / 'released-fixture')


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


def test_restricted_profile_survives_two_fresh_real_agent_processes(tmp_path, monkeypatch):
    release = tmp_path / 'release'
    skill = release / 'read-only-fixture'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text(
        '---\nname: read-only-fixture\ndescription: Read released fixture.\n---\nRELEASED_BODY\n',
        encoding='utf-8',
    )
    memories = tmp_path / 'memories'
    memories.mkdir()
    for name in ['MEMORY.md', 'USER.md']:
        (memories / name).write_text('PRIVATE_FIXTURE_SENTINEL', encoding='utf-8')
    _configure(tmp_path, monkeypatch, {
        'agent': {'environment_probe': False, 'tool_policy': {
            'version': 1, 'denied_tools': ['memory', 'skill_manage'],
            'require_explicit_scope': True,
        }},
        'skills': {'external_only': True, 'external_dirs': [str(release)], 'inline_shell': False},
    })
    script = '''
import json
from pathlib import Path
from run_agent import AIAgent, _openrouter_prewarm_done
from hermes_constants import get_hermes_home
from model_tools import handle_function_call
from tools.registry import registry
from agent.agent_runtime_helpers import invoke_tool

_openrouter_prewarm_done.set()
home = get_hermes_home()
def snapshot():
    return {str(p.relative_to(home)): p.read_bytes().hex()
            for tree in ['skills', 'pending', 'memories']
            for p in (home / tree).rglob('*') if p.is_file()}
initial_memories = {p.name: p.read_bytes() for p in (home / 'memories').glob('*.md')}
agent = AIAgent(model='test/offline-model', provider='custom', api_key='test-offline-key',
                base_url='http://127.0.0.1:9/v1', quiet_mode=True,
                skip_context_files=True, enabled_toolsets=['skills', 'memory'])
names = {t['function']['name'] for t in agent.tools}
assert names == {'skills_list', 'skill_view'}
assert 'PRIVATE_FIXTURE_SENTINEL' not in agent._build_system_prompt()
assert agent._memory_store is None and agent._memory_manager is None
assert json.loads(registry.dispatch('skills_list', {}))['skills'][0]['name'] == 'read-only-fixture'
assert 'RELEASED_BODY' in json.loads(registry.dispatch('skill_view', {'name': 'read-only-fixture'}))['content']
before = snapshot()
for action in ['create', 'patch', 'delete', 'write_file', 'remove_file', 'edit']:
    args = {'action': action, 'name': 'read-only-fixture', 'content': 'CHANGED'}
    assert 'error' in json.loads(registry.dispatch('skill_manage', args))
    assert 'error' in json.loads(handle_function_call('skill_manage', args))
    assert 'error' in json.loads(invoke_tool(agent, 'skill_manage', args, 'restart-proof'))
assert 'error' in json.loads(invoke_tool(agent, 'memory', {'action': 'add', 'target': 'memory', 'content': 'CHANGED'}, 'restart-proof'))
assert snapshot() == before
assert {p.name: p.read_bytes() for p in (home / 'memories').glob('*.md')} == initial_memories
print(json.dumps({'callable_names': sorted(names), 'trees_unchanged': True, 'private_memory_absent': True}))
'''
    results = []
    for _ in range(2):
        completed = subprocess.run(
            [sys.executable, '-c', script], env=dict(os.environ),
            capture_output=True, text=True, timeout=30,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        results.append(json.loads(completed.stdout.strip().splitlines()[-1]))
    assert results[0] == results[1]
