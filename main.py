import os
import shutil
import logging
import traceback
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

# ALLOW CORs for Vercel
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_URL", "*")], # Set this to your Vercel URL in Render ENV
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class GlobalState:
    def __init__(self):
        self.is_running = False
        self.logs = []
        self.last_run = None
        self.user_creds = None # Stores the current logged-in user credentials

state = GlobalState()

def add_log(message: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    state.logs.append(f"[{timestamp}] {message}")
    logger.info(message)

# --- OAuth2 Helpers ---

def get_google_flow():
    """Builds the OAuth flow object using environment variables."""
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

def get_current_creds():
    """Refreshes and returns the credentials of the logged-in user."""
    if not state.user_creds:
        return None
    
    creds = Credentials.from_authorized_user_info(state.user_creds, config.SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        state.user_creds = eval(creds.to_json()) # Update state with refreshed token
    return creds

# --- API Endpoints ---

@app.get("/auth/login")
def login():
    """Returns the Google Login URL."""
    flow = get_google_flow()
    authorization_url, _ = flow.authorization_url(prompt='consent', access_type='offline')
    return {"url": authorization_url}

@app.get("/auth/callback")
def auth_callback(code: str):
    """Handles the redirect from Google, exchanges code for tokens."""
    flow = get_google_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials
    
    # Store credentials in memory
    state.user_creds = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes
    }
    # Redirect back to the React Frontend
    return RedirectResponse(url=os.getenv("FRONTEND_URL"))

@app.get("/auth/status")
def get_auth_status():
    """Checks if anyone is currently logged in."""
    creds = get_current_creds()
    if creds:
        service = build('oauth2', 'v2', credentials=creds)
        user_info = service.userinfo().get().execute()
        return {"authenticated": True, "email": user_info['email']}
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
        raise HTTPException(status_code=401, detail="User not authenticated")
    if state.is_running:
        raise HTTPException(status_code=400, detail="Automation already running")
    
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
            add_log("❌ ERROR: Master Sheet is empty.")
            return

        headers = [h.strip() for h in rows[0]]
        master_df = pd.DataFrame([dict(zip(headers, r + [""]*(len(headers)-len(r)))) for r in rows[1:]])
        add_log(f"✅ Master Data Synced: {len(master_df)} employees found.")

        # 2. Pipeline Steps (Using local modules updated to accept creds)
        # Note: You should modify your modules (mail_reader.py, etc) 
        # to accept 'creds' as an argument instead of calling local get_credentials()
        
        zip_path = fetch_zip_from_mail(creds) # Pass creds here
        if not zip_path:
            add_log("⚠️ No unread 'payslip' emails found.")
            return

        pdf_folder = extract_zip(zip_path)
        
        pdf_files = []
        for root, _, filenames in os.walk(pdf_folder):
            for f in filenames:
                if f.lower().endswith(".pdf"): pdf_files.append(os.path.join(root, f))

        for pdf_path in pdf_files:
            pdf_name = os.path.basename(pdf_path)
            add_log(f"Processing: {pdf_name}")

            file_id = upload_to_drive(pdf_path, creds)
            extracted_data = extract_employee_name(pdf_path)
            
            if not extracted_data:
                add_log(f"❌ Failed to parse {pdf_name}")
                continue

            emp_name = extracted_data.get("Employee Name", "Unknown")
            month = extracted_data.get("Month", "Unknown")
            
            result = validate_employee(extracted_data, master_df)
            
            if result["status"] == "PASS":
                add_log(f"✅ Validated: {emp_name}")
                send_employee_mail(result["email"], pdf_path, emp_name, month, creds)
                move_file(file_id, config.SENT_FOLDER_ID, month, creds)
            else:
                add_log(f"❌ Validation Failed for {emp_name}: {result['reason']}")
                move_file(file_id, config.ERROR_FOLDER_ID, month, creds)

            update_report(emp_name, result, month, creds)

        add_log("🎉 ALL TASKS COMPLETED.")

    except Exception as e:
        add_log(f"🚨 SYSTEM ERROR: {str(e)}")
        logger.error(traceback.format_exc())
    finally:
        state.is_running = False
        state.last_run = datetime.now().isoformat()
        # Cleanup /tmp folders
        if os.path.exists(config.TEMP_FOLDER): shutil.rmtree(config.TEMP_FOLDER)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))