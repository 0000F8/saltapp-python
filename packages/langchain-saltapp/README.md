# langchain-saltapp

LangChain integration for [Salt](https://saltapp.ai): a `BaseToolkit`
exposing Salt's chat/payment/card tools, plus a LangGraph `interrupt()`
bridge that asks a human-in-the-loop question **in a real Salt chat** and
resumes the graph the moment they answer -- a tapped button or typed
reply, instead of a console prompt or a web inbox.

## Install

```bash
pip install "saltapp[langchain] @ git+https://github.com/0000F8/saltapp-python"
```

`langchain-saltapp` is not published to a registry yet, and it is only a re-export of `saltapp.integrations.langchain`, so install the extra from GitHub as above.

```bash
# once published:
pip install langchain-saltapp
```

## Usage

```python
# with the GitHub install above:
from saltapp.integrations.langchain import SaltToolkit, ask_via_interrupt, SaltInterruptRunner
# once langchain-saltapp is published, the same names from:
# from langchain_saltapp import SaltToolkit, ask_via_interrupt, SaltInterruptRunner
```

See the parent [`saltapp`](https://github.com/0000F8/saltapp-python) package's
`saltapp/integrations/langchain.py` for the full API (this package is a
thin re-export -- all the logic lives there, so the two stay in lockstep)
and `saltapp-python`'s `examples/langgraph_interrupt.py` for a runnable
cookbook: a graph that pauses, asks a human on Salt with buttons, and
resumes on the tap.

## Links

- [saltapp.ai/developers](https://saltapp.ai/developers)
- [saltapp-python](https://github.com/0000F8/saltapp-python) (this package's source, under `packages/langchain-saltapp/`)
