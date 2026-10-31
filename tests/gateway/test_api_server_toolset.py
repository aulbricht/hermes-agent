"""Tests for hermes-api-server toolset and API server tool availability."""
from unittest.mock import patch, MagicMock


from toolsets import resolve_toolset, get_toolset, validate_toolset


class TestHermesApiServerToolset:
    """Tests for the hermes-api-server toolset definition."""

    def test_toolset_exists(self):
        ts = get_toolset("hermes-api-server")
        assert ts is not None

    def test_toolset_validates(self):
        assert validate_toolset("hermes-api-server")

    def test_toolset_includes_web_tools(self):
        tools = resolve_toolset("hermes-api-server")
        assert "web_search" in tools
        assert "web_extract" in tools

    def test_toolset_includes_core_tools(self):
        tools = resolve_toolset("hermes-api-server")
        expected = [
            "terminal", "process",
            "read_file", "write_file", "patch", "search_files",
            "vision_analyze", "image_generate",
            "execute_code", "delegate_task",
            "todo", "memory", "session_search", "cronjob",
        ]
        for tool in expected:
            assert tool in tools, f"Missing expected tool: {tool}"

    def test_toolset_includes_browser_tools(self):
        tools = resolve_toolset("hermes-api-server")
        for tool in ["browser_navigate", "browser_snapshot", "browser_click",
                      "browser_type", "browser_scroll", "browser_back",
                      "browser_press"]:
            assert tool in tools, f"Missing browser tool: {tool}"

    def test_toolset_includes_homeassistant_tools(self):
        tools = resolve_toolset("hermes-api-server")
        for tool in ["ha_list_entities", "ha_get_state", "ha_list_services", "ha_call_service"]:
            assert tool in tools, f"Missing HA tool: {tool}"

    def test_toolset_excludes_clarify(self):
        tools = resolve_toolset("hermes-api-server")
        assert "clarify" not in tools

    def test_toolset_excludes_send_message(self):
        tools = resolve_toolset("hermes-api-server")
        assert "send_message" not in tools

    def test_toolset_excludes_text_to_speech(self):
        tools = resolve_toolset("hermes-api-server")
        assert "text_to_speech" not in tools


class TestApiServerPlatformConfig:
    def test_platforms_dict_includes_api_server(self):
        from hermes_cli.tools_config import PLATFORMS
        assert "api_server" in PLATFORMS
        assert PLATFORMS["api_server"]["default_toolset"] == "hermes-api-server"

    def test_default_api_server_includes_terminal_toolset(self):
        """Regression #49622: desktop-only read_terminal is registered into the
        'terminal' toolset (ships in-repo), so resolve_toolset('terminal') grows
        to include it after discovery. read_terminal is NOT in the
        hermes-api-server composite, so the old all-tools subset test dropped
        'terminal' entirely. Its static membership (terminal, process) IS in the
        composite, so it must stay enabled."""
        from tools.registry import discover_builtin_tools
        from hermes_cli.tools_config import _get_platform_tools
        discover_builtin_tools()
        assert "terminal" in _get_platform_tools({}, "api_server")

    def test_registering_tool_into_toolset_does_not_drop_toolset_from_inference(self):
        """Class invariant (covers the delegate_cli overlay case): registering a
        NEW tool into an existing configurable toolset must never remove that
        toolset from a platform whose composite lists the toolset's static
        tools. Synthetic registration keeps the test hermetic in CI."""
        from tools.registry import registry
        from hermes_cli.tools_config import _get_platform_tools

        sentinel = "test_sentinel_delegation_tool"
        registry.register(
            name=sentinel,
            toolset="delegation",
            schema={"name": sentinel, "description": "test",
                    "parameters": {"type": "object", "properties": {}}},
            handler=lambda args, **kw: "{}",
        )
        try:
            # delegation's static membership (delegate_task) is in the composite,
            # so the toolset must survive inference despite the extra registry tool.
            assert "delegation" in _get_platform_tools({}, "api_server"), (
                "registering a tool into 'delegation' dropped it from api_server"
            )
        finally:
            registry.deregister(sentinel)

    def test_default_off_and_restricted_toolsets_stay_off_on_api_server(self):
        """Negative contract: the static-membership comparison must NOT newly
        enable default-off or platform-restricted toolsets."""
        import os
        from unittest.mock import patch
        from hermes_cli.tools_config import _get_platform_tools
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HASS_TOKEN", None)
            os.environ.pop("XAI_API_KEY", None)
            enabled = _get_platform_tools({}, "api_server")
        assert "homeassistant" not in enabled
        assert "discord" not in enabled
        assert "discord_admin" not in enabled
        assert "x_search" not in enabled


class TestApiServerAdapterToolset:
    @patch("gateway.platforms.api_server.AIOHTTP_AVAILABLE", True)
    def test_ownership_luna_budget_reaches_responses_request(self):
        from types import SimpleNamespace

        from agent.chat_completion_helpers import build_api_kwargs
        from agent.context_compressor import ContextCompressor
        from agent.transports.codex import ResponsesApiTransport
        from gateway.config import PlatformConfig
        from gateway.platforms.api_server import APIServerAdapter

        adapter = APIServerAdapter(PlatformConfig())
        transport = ResponsesApiTransport()

        def construct(**settings):
            settings.setdefault("max_tokens", 8192)
            # Real compressor with the retained Atlas profile's old window.
            # No provider discovery or paid call is needed for an explicit window.
            compressor = ContextCompressor(
                model=settings["model"], config_context_length=100000,
                max_tokens=settings["max_tokens"], quiet_mode=True,
                base_url=settings["base_url"], provider=settings["provider"],
            )
            return SimpleNamespace(**settings, context_compressor=compressor)

        with patch("gateway.run._resolve_runtime_agent_kwargs", side_effect=lambda: {
                 "api_key": "test-key", "base_url": "https://openrouter.ai/api/v1",
                 "provider": "openrouter", "api_mode": "codex_responses"}), \
             patch("gateway.run._resolve_gateway_model", return_value="openai/gpt-6-luna"), \
             patch("gateway.run._load_gateway_config", return_value={}), \
             patch("gateway.run.GatewayRunner._load_reasoning_config", return_value={"enabled": True, "effort": "xhigh"}), \
             patch("hermes_cli.tools_config._get_platform_tools", return_value={"web"}), \
             patch("run_agent.AIAgent", side_effect=construct) as constructor:
            ownership = adapter._create_agent(atlas_ownership_authorized=True)
            compressor = ownership.context_compressor
            assert compressor.context_length == ownership._config_context_length
            # Keep every input below Luna's long-context price step, even when
            # compaction is deferred; reserve the unchanged output allowance.
            input_budget = compressor.context_length - ownership.max_tokens
            assert 100000 < compressor.threshold_tokens < input_budget <= 272000
            assert compressor.threshold_tokens == int(input_budget * 0.75)
            # Synthetic heavy evidence case, not a measured live turn.
            compressor.last_prompt_tokens = 100000
            assert not compressor.should_compress()
            compressor.last_prompt_tokens = compressor.threshold_tokens
            assert compressor.should_compress()
            settings = constructor.call_args.kwargs
            assert settings["enabled_toolsets"] == ["atlas_ownership"]
            agent = SimpleNamespace(
                **settings,
                tools=[], request_overrides={},
                _base_url_hostname="openrouter.ai", _base_url_lower="https://openrouter.ai/api/v1",
                _get_transport=lambda: transport,
                _prepare_messages_for_non_vision_model=lambda messages: messages,
                _resolved_api_call_timeout=lambda: 600,
            )
            payload = transport.preflight_kwargs(build_api_kwargs(
                agent, [{"role": "user", "content": "Save the ownership proposal."}]
            ))
            assert payload["max_output_tokens"] == 65536
            assert payload["reasoning"]["effort"] == "medium"

            # The nightly caller selects the atlas-luna route, whose xhigh
            # effort must not undo the authenticated ownership policy.
            with patch("gateway.run._resolve_runtime_agent_kwargs_for_provider", return_value={
                "api_key": "test-key", "base_url": "https://openrouter.ai/api/v1",
                "provider": "openrouter", "api_mode": "codex_responses",
            }):
                routed = adapter._create_agent(atlas_ownership_authorized=True, route={
                    "model": "openai/gpt-6-luna", "provider": "openrouter", "reasoning_effort": "xhigh",
                })
            assert routed.context_compressor.threshold_tokens == compressor.threshold_tokens
            assert routed.reasoning_config["effort"] == "medium"

            # The authenticated ownership selector is required; neither an
            # ordinary turn nor the existing import path inherits the budget.
            for selectors in ({}, {"atlas_import_authorized": True}):
                ordinary = adapter._create_agent(**selectors)
                settings = constructor.call_args.kwargs
                assert "max_tokens" not in settings
                assert settings["reasoning_config"]["effort"] == "xhigh"
                assert ordinary.context_compressor.context_length == 100000

            # Another selected model keeps its own output/reasoning policy.
            with patch("gateway.run._resolve_gateway_model", return_value="openai/gpt-6.1-sol"):
                adapter._create_agent(atlas_ownership_authorized=True)
                assert "max_tokens" not in constructor.call_args.kwargs
                assert constructor.call_args.kwargs["reasoning_config"]["effort"] == "xhigh"

            with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={
                "api_key": "test-key", "base_url": "https://api.openai.com/v1",
                "provider": "openai", "api_mode": "codex_responses",
            }):
                other_provider = adapter._create_agent(atlas_ownership_authorized=True)
            assert other_provider.max_tokens == 8192
            assert other_provider.context_compressor.context_length == 100000

    @patch("gateway.platforms.api_server.AIOHTTP_AVAILABLE", True)
    def test_create_agent_turn_scope_only_narrows_configured_tools(self):
        from gateway.platforms.api_server import APIServerAdapter
        from gateway.config import PlatformConfig

        adapter = APIServerAdapter(PlatformConfig())
        with patch("gateway.run._resolve_runtime_agent_kwargs") as mock_kwargs, \
             patch("gateway.run._resolve_gateway_model", return_value="test/model"), \
             patch("gateway.run._load_gateway_config", return_value={
                 "platform_toolsets": {"api_server": ["web", "terminal"]}
             }), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_kwargs.return_value = {"api_key": "test-key", "base_url": None,
                                        "provider": None, "api_mode": None,
                                        "command": None, "args": []}
            adapter._create_agent(toolset_scope=["web", "not-configured"])
            assert mock_agent_cls.call_args.kwargs["enabled_toolsets"] == ["web"]
            adapter._create_agent()
            assert mock_agent_cls.call_args.kwargs["enabled_toolsets"] == ["terminal", "web"]
            adapter._create_agent(atlas_import_authorized=True)
            assert mock_agent_cls.call_args.kwargs["enabled_toolsets"] == ["terminal", "web", "atlas_import"]

    @patch("gateway.platforms.api_server.AIOHTTP_AVAILABLE", True)
    def test_import_tool_stays_off_even_if_platform_config_lists_it(self):
        from gateway.platforms.api_server import APIServerAdapter
        from gateway.config import PlatformConfig

        adapter = APIServerAdapter(PlatformConfig())
        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={
                 "api_key": "test-key", "base_url": None, "provider": None,
                 "api_mode": None, "command": None, "args": []}), \
             patch("gateway.run._resolve_gateway_model", return_value="test/model"), \
             patch("gateway.run._load_gateway_config", return_value={}), \
             patch("hermes_cli.tools_config._get_platform_tools", return_value={"web", "atlas_import"}), \
             patch("run_agent.AIAgent") as agent:
            adapter._create_agent()
            assert agent.call_args.kwargs["enabled_toolsets"] == ["web"]
            adapter._create_agent(atlas_import_authorized=True)
            assert agent.call_args.kwargs["enabled_toolsets"] == ["web", "atlas_import"]

    @patch("gateway.platforms.api_server.AIOHTTP_AVAILABLE", True)
    def test_create_agent_reads_config_toolsets(self):
        """API server resolves toolsets from config like all other platforms."""
        from gateway.platforms.api_server import APIServerAdapter
        from gateway.config import PlatformConfig

        adapter = APIServerAdapter(PlatformConfig())

        with patch("gateway.run._resolve_runtime_agent_kwargs") as mock_kwargs, \
             patch("gateway.run._resolve_gateway_model") as mock_model, \
             patch("gateway.run._load_gateway_config") as mock_config, \
             patch("run_agent.AIAgent") as mock_agent_cls:

            mock_kwargs.return_value = {"api_key": "test-key", "base_url": None,
                                        "provider": None, "api_mode": None,
                                        "command": None, "args": []}
            mock_model.return_value = "test/model"
            # No platform_toolsets override — should fall back to hermes-api-server default
            mock_config.return_value = {}
            mock_agent_cls.return_value = MagicMock()

            adapter._create_agent()

            mock_agent_cls.assert_called_once()
            call_kwargs = mock_agent_cls.call_args
            toolsets = call_kwargs.kwargs.get("enabled_toolsets")
            assert isinstance(toolsets, list)
            assert len(toolsets) > 0
            assert call_kwargs.kwargs.get("platform") == "api_server"

    @patch("gateway.platforms.api_server.AIOHTTP_AVAILABLE", True)
    def test_create_agent_respects_config_override(self):
        """User can override API server toolsets via platform_toolsets in config.yaml."""
        from gateway.platforms.api_server import APIServerAdapter
        from gateway.config import PlatformConfig

        adapter = APIServerAdapter(PlatformConfig())

        with patch("gateway.run._resolve_runtime_agent_kwargs") as mock_kwargs, \
             patch("gateway.run._resolve_gateway_model") as mock_model, \
             patch("gateway.run._load_gateway_config") as mock_config, \
             patch("run_agent.AIAgent") as mock_agent_cls:

            mock_kwargs.return_value = {"api_key": "test-key", "base_url": None,
                                        "provider": None, "api_mode": None,
                                        "command": None, "args": []}
            mock_model.return_value = "test/model"
            # User overrides with just web and terminal
            mock_config.return_value = {
                "platform_toolsets": {"api_server": ["web", "terminal"]}
            }
            mock_agent_cls.return_value = MagicMock()

            adapter._create_agent()

            mock_agent_cls.assert_called_once()
            call_kwargs = mock_agent_cls.call_args
            toolsets = call_kwargs.kwargs.get("enabled_toolsets")
            assert sorted(toolsets) == ["terminal", "web"]
