import config
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# In production, use a Database (Postgres/Redis). 
# For now, we'll use a global variable (reset on restart).
USER_CREDENTIALS = {} 

def get_flow():
    return Flow.from_client_config(
        config.GOOGLE_CLIENT_CONFIG,
        scopes=config.SCOPES,
        redirect_uri=f"{config.BACKEND_URL}/auth/callback"
    )

def get_credentials():
    """Returns credentials for the currently logged-in user."""
    # This logic assumes a single-user production setup. 
    # For multi-user, you'd pass a session ID here.
    if not USER_CREDENTIALS:
        return None
    
    creds = Credentials.from_authorized_user_info(USER_CREDENTIALS, config.SCOPES)
    
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # Update our store
        global USER_CREDENTIALS
        USER_CREDENTIALS = creds.to_json()
        
    return creds