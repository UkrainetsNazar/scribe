import os

# The known development JWT secret is accepted only when APP_ENV=test.
# This runs before anything imports scribe.config.
os.environ["APP_ENV"] = "test"
