import config
from datetime import datetime
from googleapiclient.discovery import build

def update_report(name, result, month_name, creds):
    try:
        # Initialize service using the credentials passed from main.py
        service = build('sheets', 'v4', credentials=creds)
        spreadsheet_id = config.LOGGER_SHEET_ID

        # 1. Check if the Month Tab exists
        sheet_metadata = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheets = sheet_metadata.get('sheets', [])
        sheet_exists = any(s['properties']['title'] == month_name for s in sheets)

        if not sheet_exists:
            # Create tab if it doesn't exist
            request_body = {'requests': [{'addSheet': {'properties': {'title': month_name}}}]}
            service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=request_body).execute()
            
            # Add Headers
            headers = [["Timestamp", "Employee Name", "Status", "Details", "Mail ID"]]
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{month_name}'!A1:E1",
                valueInputOption="USER_ENTERED",
                body={'values': headers}
            ).execute()

        # 2. Append the log entry
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        values = [[
            timestamp,
            name if name else "Unknown",
            result["status"],
            result.get("reason", "N/A"),
            result.get("email", "N/A")
        ]]

        service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"'{month_name}'!A:E",
            valueInputOption="USER_ENTERED",
            body={'values': values}
        ).execute()

    except Exception as e:
        print(f"   - Error logging to sheet: {e}")