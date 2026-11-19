"""Profile-local tool authorization shared by schemas and all dispatch paths."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import json
import re

from hermes_constants import get_hermes_home


@dataclass(frozen=True)
class ProfileToolPolicy:
    version: int
    denied_tools: frozenset[str]
    require_explicit_scope: bool
    profile_path: str


_active_policy = ContextVar('profile_tool_policy', default=None)
_active_tools = ContextVar('profile_callable_names', default=None)


def active_callable_names():
    """The current turn's immutable ceiling, also used to isolate schema caches."""
    return _active_tools.get()


def get_profile_tool_policy():
    """Read a fresh policy; malformed explicit configuration never grants access."""
    import yaml
    home = get_hermes_home().resolve()
    try:
        config = yaml.safe_load((home / 'config.yaml').read_text(encoding='utf-8')) or {}
    except FileNotFoundError:
        config = {}
    if not isinstance(config, dict):
        raise ValueError('config.yaml must be a mapping')
    agent = config.get('agent', {})
    if not isinstance(agent, dict):
        raise ValueError('agent must be a mapping')
    if 'tool_policy' not in agent:
        return ProfileToolPolicy(1, frozenset(), False, str(home))
    policy = agent['tool_policy']
    if not isinstance(policy, dict) or type(policy.get('version')) is not int or policy['version'] != 1:
        raise ValueError('agent.tool_policy requires version: 1')
    denied = policy.get('denied_tools', [])
    explicit = policy.get('require_explicit_scope', False)
    if (not isinstance(denied, list) or type(explicit) is not bool
            or any(not isinstance(n, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]+', n) for n in denied)
            or set(policy) - {'version', 'denied_tools', 'require_explicit_scope'}):
        raise ValueError('Invalid agent.tool_policy')
    return ProfileToolPolicy(1, frozenset(denied), explicit, str(home))


def effective_tool_policy(agent=None):
    policy = getattr(agent, '_tool_policy', None) if agent is not None else None
    return policy if isinstance(policy, ProfileToolPolicy) else (_active_policy.get() or get_profile_tool_policy())


def memory_allowed(agent=None):
    return 'memory' not in effective_tool_policy(agent).denied_tools


def tool_refusal(name, *, agent=None, allowed_tools=None):
    policy = effective_tool_policy(agent)
    denied = name in policy.denied_tools
    if agent is not None and not memory_allowed(agent):
        manager = getattr(agent, '_memory_manager', None)
        denied = denied or bool(manager and manager.has_tool(name))
    ceiling = _active_tools.get()
    if agent is not None:
        allowed_tools = getattr(agent, 'valid_tool_names', allowed_tools)
    if denied or (ceiling is not None and name not in ceiling) or (allowed_tools is not None and name not in allowed_tools):
        return json.dumps({'success': False, 'error': f"Tool '{name}' is not permitted by the profile or conversation scope"})
    return None


@contextmanager
def active_tool_policy(policy, allowed_tools=None):
    """Nested bindings only narrow the active conversation's authority."""
    outer_policy = _active_policy.get()
    if outer_policy is not None:
        policy = ProfileToolPolicy(policy.version, policy.denied_tools | outer_policy.denied_tools,
                                   policy.require_explicit_scope or outer_policy.require_explicit_scope,
                                   policy.profile_path)
    outer_tools = _active_tools.get()
    names = frozenset(allowed_tools) if allowed_tools is not None else outer_tools
    if outer_tools is not None and names is not None:
        names &= outer_tools
    token = _active_policy.set(policy)
    tool_token = _active_tools.set(names)
    try:
        yield
    finally:
        _active_tools.reset(tool_token)
        _active_policy.reset(token)


def scoped_agent_tools(fn):
    """Bind the frozen agent policy, including for direct executor callers."""
    @wraps(fn)
    def scoped(agent, *args, **kwargs):
        with active_tool_policy(effective_tool_policy(agent), getattr(agent, 'valid_tool_names', None)):
            return fn(agent, *args, **kwargs)
    return scoped


def initialize_tool_policy(fn):
    @wraps(fn)
    def initialize(agent, *args, **kwargs):
        agent._tool_policy = get_profile_tool_policy()
        from agent.skill_utils import external_only_skills, approved_skill_roots
        if external_only_skills() and not approved_skill_roots():
            raise ValueError('skills.external_only requires usable skills.external_dirs')
        with active_tool_policy(agent._tool_policy):
            result = fn(agent, *args, **kwargs)
            agent.tools = [t for t in agent.tools if tool_refusal(t['function']['name']) is None]
            names = {t['function']['name'] for t in agent.tools}
            from tools.tool_search import BRIDGE_TOOL_NAMES
            if names & BRIDGE_TOOL_NAMES:
                # Deferred tools remain callable through the bridge. Freeze the
                # authorized underlying catalog at initialization as well.
                from model_tools import get_tool_definitions
                catalog = get_tool_definitions(
                    enabled_toolsets=agent.enabled_toolsets,
                    disabled_toolsets=agent.disabled_toolsets,
                    quiet_mode=True, skip_tool_search_assembly=True,
                )
                names.update(t['function']['name'] for t in catalog)
            agent.valid_tool_names = frozenset(names)
            return result
    return initialize
