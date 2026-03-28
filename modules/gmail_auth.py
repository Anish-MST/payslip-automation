import os
import config
from google_auth_oauthlib.flow import Flow

def get_flow():
    """Builds the OAuth flow object using environment variables."""
    # We use client_config dict instead of a physical JSON file for Render
    client_config = {
        "web": {
            "client_id": os.getenv("GOOGLE_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [f"{os.getenv('BACKEND_URL')}/auth/callback"]
        }
    }
    return Flow.from_client_config(
        client_config,
        scopes=config.SCOPES
    )