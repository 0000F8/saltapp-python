# llama-index-tools-saltapp

LlamaIndex tool spec for [Salt](https://saltapp.ai): send messages, ask a
human-in-the-loop question, request payments/invoices, post cards, and
check payment status, in a real Salt chat.

## Install

```bash
pip install "saltapp[llamaindex] @ git+https://github.com/0000F8/saltapp-python"
```

`llama-index-tools-saltapp` is not published to a registry yet, and it is only a re-export of `saltapp.integrations.llamaindex`, so install the extra from GitHub as above.

```bash
# once published:
pip install llama-index-tools-saltapp
```

## Usage

```python
from llama_index.tools.saltapp import SaltToolSpec

tool_spec = SaltToolSpec(agent=salt_agent, chat_id=chat_id)
agent = FunctionAgent(llm=llm, tools=tool_spec.to_tool_list())
```

See the parent [`saltapp`](https://github.com/0000F8/saltapp-python) package's
`saltapp/integrations/llamaindex.py` for the full API (this package is a
thin re-export -- all the logic lives there, so the two stay in lockstep)
and `saltapp-python`'s `examples/llamaindex_agent.py` for a runnable
cookbook.

## Links

- [saltapp.ai/developers](https://saltapp.ai/developers)
- [saltapp-python](https://github.com/0000F8/saltapp-python) (this package's source, under `packages/llama-index-tools-saltapp/`)
