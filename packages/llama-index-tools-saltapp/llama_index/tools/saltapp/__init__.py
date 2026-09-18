"""llama-index-tools-saltapp -- the LlamaHub-namespaced entry point for
Salt's (saltapp.ai) LlamaIndex integration.

All logic lives in `saltapp.integrations.llamaindex` (the parent `saltapp`
package, this package's one runtime dependency) -- this module just
re-exports it under the `llama_index.tools.saltapp` import path LlamaHub's
`llama-index-tools-<name>` convention expects:

    pip install llama-index-tools-saltapp
    from llama_index.tools.saltapp import SaltToolSpec

    tool_spec = SaltToolSpec(agent=salt_agent, chat_id=chat_id)
    agent = FunctionAgent(llm=llm, tools=tool_spec.to_tool_list())
"""

from __future__ import annotations

from saltapp.integrations.llamaindex import SaltToolSpec

__all__ = ["SaltToolSpec"]
