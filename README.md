# FN Console (Render deploy)

Chat + coding agent + encrypted session archive. Pure-stdlib Python,
Docker runtime, listens on $PORT (Render injects it).

Set these env vars in the Render dashboard (Secrets):
- APP_PASSWORD      — access password (login screen)
- ADMIN_PASSWORD    — admin log decrypt password
- ONEPROVIDER_KEY   — OneProvider API key
- FN_STATIC_KEY     — optional static API key (header x-api-key)

Free-tier note: the service spins down after ~15 min idle; first request
after that takes ~50s to cold-start. Workspace/archive are ephemeral.
