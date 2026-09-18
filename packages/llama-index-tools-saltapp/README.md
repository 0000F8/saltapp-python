# llama-index-tools-saltapp

LlamaIndex tool spec for [Salt](https://saltapp.ai): send messages, ask a
human-in-the-loop question, request payments/invoices, post cards, and
check payment status, in a real Salt chat.

## Install

```bash
pip install llama-index-tools-saltapp
```

## Usage

```python
from llama_index.tools.saltapp import SaltToolSpec

tool_spec = SaltToolSpec(agent=salt_agent, chat_id=chat_id)
agent = FunctionAgent(llm=llm, tools=tool_spec.to_tool_list())
```

See the parent [`saltapp`](https://pypi.org/project/saltapp/) package's
`saltapp/integrations/llamaindex.py` for the full API (this package is a
thin re-export -- all the logic lives there, so the two stay in lockstep)
and `saltapp-python`'s `examples/llamaindex_agent.py` for a runnable
cookbook.

## Links

- [saltapp.ai/developers](https://saltapp.ai/developers)
- [saltapp on PyPI](https://pypi.org/project/saltapp/)
- [saltapp-python](https://github.com/0000F8/saltapp-python) (this package's source, under `packages/llama-index-tools-saltapp/`)
