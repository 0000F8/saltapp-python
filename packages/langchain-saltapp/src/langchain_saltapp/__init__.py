"""langchain-saltapp -- the LangChain-namespaced entry point for Salt's
(saltapp.ai) LangChain/LangGraph integration.

All logic lives in `saltapp.integrations.langchain` (the parent `saltapp`
package, this package's one runtime dependency) -- this module just
re-exports it under the `langchain_saltapp` import name LangChain's docs
list partner packages under, so a person following LangChain's own
integration conventions finds this exactly where they'd expect:

    pip install langchain-saltapp
    from langchain_saltapp import SaltToolkit, ask_via_interrupt, SaltInterruptRunner
"""

from __future__ import annotations

from saltapp.integrations.langchain import SaltInterruptRunner, SaltToolkit, ask_via_interrupt

__all__ = ["SaltToolkit", "ask_via_interrupt", "SaltInterruptRunner"]
