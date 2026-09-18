# Thin, optional adapters mounting a saltapp.agent.Agent's webhook handling
# into an existing FastAPI/Starlette or Flask app. Neither fastapi nor flask
# is a hard dependency of saltapp -- install `saltapp[fastapi]` or
# `saltapp[flask]` to pull in the one you need. If you don't already have
# one of those apps, `Agent.asgi_app()` needs neither: it's a plain ASGI3
# callable you can run directly under uvicorn.
