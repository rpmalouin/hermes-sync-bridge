#!/bin/sh
# Cron wrapper: the Hermes cron 'script' field takes a path, not arguments, so the
# flags live here. --quiet keeps an idle inbox silent (the no_agent contract: empty
# stdout sends nothing); a run that reads mail still prints one line per message.
exec /usr/bin/python3 /root/.hermes/scripts/hermes-docker-inbox.py --quiet
