# langchain-saltapp

LangChain integration for [Salt](https://saltapp.ai): a `BaseToolkit`
exposing Salt's chat/payment/card tools, plus a LangGraph `interrupt()`
bridge that asks a human-in-the-loop question **in a real Salt chat** and
resumes the graph the moment they answer -- a tapped button or typed
reply, instead of a console prompt or a web inbox.

## Install

```bash
pip install langchain-saltapp
```

## Usage

```python
from langchain_saltapp import SaltToolkit, ask_via_interrupt, SaltInterruptRunner
```

See the parent [`saltapp`](https://pypi.org/project/saltapp/) package's
`saltapp/integrations/langchain.py` for the full API (this package is a
thin re-export -- all the logic lives there, so the two stay in lockstep)
and `saltapp-python`'s `examples/langgraph_interrupt.py` for a runnable
cookbook: a graph that pauses, asks a human on Salt with buttons, and
resumes on the tap.

## Links

- [saltapp.ai/developers](https://saltapp.ai/developers)
- [saltapp on PyPI](https://pypi.org/project/saltapp/)
- [saltapp-python](https://github.com/0000F8/saltapp-python) (this package's source, under `packages/langchain-saltapp/`)
