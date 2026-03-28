import os
import shutil
import logging
import traceback
import json
from datetime import datetime
from typing import List

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
import pandas as pd
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest

# Local Modules
import config
from modules.mail_reader import fetch_zip_from_mail
from modules.zip_handler import extract_zip
from modules.drive_manager import upload_to_drive, move_file
from modules.pdf_parser import extract_employee_name 
from modules.validator import validate_employee      
from modules.sheet_logger import update_report
from modules.mail_sender import send_employee_mail

# --- Configuration & Global State ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("payslip_automation")

app = FastAPI(title="MainstreamTek Production Pipeline")

# ALLOW CORS
# In production, os.getenv("FRONTEND_URL") should be your Vercel URL
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # For production, change to [os.getenv("FRONTEND_URL")]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class GlobalState:
    def __init__(self):
        self.is_running = False
        self.logs = []
        self.last_run = None
        self.user_creds = None # Stores credentials in memory

state = GlobalState()

def add_log(message: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    state.logs.append(f"[{timestamp}] {message}")
    logger.info(message)

# --- OAuth2 Helpers ---

def get_google_flow():
    """Builds the OAuth flow object and explicitly sets the redirect URI."""
    backend_url = os.getenv("BACKEND_URL", "http://localhost:8000")
    redirect_uri = f"{backend_url}/auth/callback"
    
    client_config = {
        "web": {
            "client_id": os.getenv("GOOGLE_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    
    flow = Flow.from_client_config(
        client_config,
        scopes=config.SCOPES
    )
    
    # CRITICAL: This must match the URI registered in Google Cloud Console
    flow.redirect_uri = redirect_uri
    return flow

def get_current_creds():
    """Refreshes and returns the credentials of the logged-in user."""
    if not state.user_creds:
        return None
    
    creds = Credentials.from_authorized_user_info(state.user_creds, config.SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        # Update the stored creds with the new token
        state.user_creds = json.loads(creds.to_json())
    return creds

# --- API Endpoints ---

@app.get("/auth/login")
def login():
    """Returns the Google Login URL."""
    try:
        flow = get_google_flow()
        authorization_url, _ = flow.authorization_url(
            prompt='consent', 
            access_type='offline',
            include_granted_scopes='true'
        )
        return {"url": authorization_url}
    except Exception as e:
        logger.error(f"Login URL Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/auth/callback")
def auth_callback(code: str):
    """Handles the redirect from Google, exchanges code for tokens."""
    try:
        flow = get_google_flow()
        flow.fetch_token(code=code)
        creds = flow.credentials
        
        # Store credentials in dictionary format
        state.user_creds = json.loads(creds.to_json())
        
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
        return RedirectResponse(url=frontend_url)
    except Exception as e:
        logger.error(f"Callback Error: {e}")
        return {"error": "Authentication failed", "details": str(e)}

@app.get("/auth/status")
def get_auth_status():
    """Checks if anyone is currently logged in and returns their email."""
    creds = get_current_creds()
    if creds:
        try:
            service = build('oauth2', 'v2', credentials=creds)
            user_info = service.userinfo().get().execute()
            return {"authenticated": True, "email": user_info['email']}
        except Exception:
            return {"authenticated": False}
    return {"authenticated": False}

@app.get("/status")
def get_pipeline_status():
    return {
        "is_running": state.is_running,
        "logs": state.logs[-50:], 
        "last_run": state.last_run
    }

@app.post("/start")
def start_process(background_tasks: BackgroundTasks):
    creds = get_current_creds()
    if not creds:
        raise HTTPException(status_code=401, detail="User not authenticated. Please login first.")
    if state.is_running:
        raise HTTPException(status_code=400, detail="Automation is already running")
    
    background_tasks.add_task(run_automation_pipeline, creds)
    return {"message": "Pipeline started"}

# --- Pipeline Logic ---

def run_automation_pipeline(creds):
    state.is_running = True
    state.logs = []
    add_log("🚀 INITIALIZING PRODUCTION PIPELINE...")

    try:
        # 1. Fetch Master Data
        sheets_service = build('sheets', 'v4', credentials=creds)
        sheet_result = sheets_service.spreadsheets().values().get(
            spreadsheetId=config.MASTER_SHEET_ID,
            range="Sheet1!A1:Z100" 
        ).execute()
        
        rows = sheet_result.get('values', [])
        if len(rows) < 2:
            add_log("❌ ERROR: Master Sheet is empty or headers missing.")
            return

        headers = [h.strip() for h in rows[0]]
        master_data = []
        for r in rows[1:]:
            # Ensure row length matches headers to prevent zip issues
            padded_row = r + [""] * (len(headers) - len(r))
            master_data.append(dict(zip(headers, padded_row)))
            
        master_df = pd.DataFrame(master_data)
        add_log(f"✅ Master Data Synced: {len(master_df)} employees found.")

        # 2. Scan Gmail
        add_log("📬 Scanning Gmail for 'payslip' attachments...")
        zip_path = fetch_zip_from_mail(creds)
        
        if not zip_path:
            add_log("⚠️ No unread 'payslip' emails with ZIP found.")
            return

        # 3. Handle Files
        pdf_folder = extract_zip(zip_path)
        pdf_files = []
        for root, _, filenames in os.walk(pdf_folder):
            for f in filenames:
                if f.lower().endswith(".pdf"):
                    pdf_files.append(os.path.join(root, f))

        add_log(f"🔎 Found {len(pdf_files)} PDF(s) in ZIP.")

        # 4. Processing Loop
        for pdf_path in pdf_files:
            pdf_name = os.path.basename(pdf_path)
            add_log(f"--- Processing: {pdf_name} ---")

            # Upload & Parse
            file_id = upload_to_drive(pdf_path, creds)
            extracted_data = extract_employee_name(pdf_path)
            
            if not extracted_data:
                add_log(f"   - ❌ Failed to parse PDF text.")
                continue

            emp_name = extracted_data.get("Employee Name", "Unknown")
            month = extracted_data.get("Month", "Unknown")
            
            # Validation
            result = validate_employee(extracted_data, master_df)
            
            if result["status"] == "PASS":
                add_log(f"   - ✅ PASS: {emp_name}")
                send_employee_mail(result["email"], pdf_path, emp_name, month, creds)
                move_file(file_id, config.SENT_FOLDER_ID, month, creds)
            else:
                add_log(f"   - ❌ FAIL: {result.get('reason')}")
                move_file(file_id, config.ERROR_FOLDER_ID, month, creds)

            # Log to Sheets
            update_report(emp_name, result, month, creds)

        add_log("🎉 ALL TASKS COMPLETED SUCCESSFULLY.")

    except Exception as e:
        add_log(f"🚨 SYSTEM ERROR: {str(e)}")
        logger.error(traceback.format_exc())
    finally:
        state.is_running = False
        state.last_run = datetime.now().isoformat()
        
        # Cleanup
        try:
            if os.path.exists(config.TEMP_FOLDER): shutil.rmtree(config.TEMP_FOLDER)
            if os.path.exists(config.EXTRACTED_FOLDER): shutil.rmtree(config.EXTRACTED_FOLDER)
            add_log("🧹 Cleanup complete.")
        except Exception as cleanup_err:
            logger.error(f"Cleanup error: {cleanup_err}")

if __name__ == "__main__":
    import uvicorn
    # Render provides PORT via env
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)