import config
from datetime import datetime, timedelta, timezone
from googleapiclient.discovery import build

def get_ist_time():
    """Returns current time in Indian Standard Time (IST)."""
    # UTC to IST is +5 hours and 30 minutes
    ist_timezone = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist_timezone).strftime("%Y-%m-%d %H:%M:%S")

def get_already_passed_employees(month_name, creds):
    try:
        service = build('sheets', 'v4', credentials=creds)
        result = service.spreadsheets().values().get(
            spreadsheetId=config.LOGGER_SHEET_ID,
            range=f"'{month_name}'!A:C"
        ).execute()
        rows = result.get('values', [])
        if not rows: return []
        # Return names where Column C is PASS
        return [row[1] for row in rows if len(row) > 2 and row[2] == "PASS"]
    except Exception:
        return []

def update_report(name, result, month_name, creds):
    """
    Logs result to Google Sheets. 
    If employee already exists in the sheet, it overwrites their row.
    """
    try:
        service = build('sheets', 'v4', credentials=creds)
        spreadsheet_id = config.LOGGER_SHEET_ID
        timestamp = get_ist_time()

        # 1. Ensure tab exists and get existing data
        try:
            sheet_data = service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=f"'{month_name}'!A:E"
            ).execute()
            rows = sheet_data.get('values', [])
        except Exception:
            # Create tab if missing
            request_body = {'requests': [{'addSheet': {'properties': {'title': month_name}}}]}
            service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=request_body).execute()
            # Add Headers
            headers = [["Timestamp (IST)", "Employee Name", "Status", "Details", "Mail ID"]]
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id, range=f"'{month_name}'!A1:E1",
                valueInputOption="USER_ENTERED", body={'values': headers}
            ).execute()
            rows = []

        # 2. Check if employee already exists in the current month's sheet
        existing_row_index = -1
        for i, row in enumerate(rows):
            if len(row) > 1 and row[1] == name:
                existing_row_index = i + 1 # Sheets are 1-indexed
                break

        row_values = [[
            timestamp,
            name,
            result["status"],
            result.get("reason", "N/A"),
            result.get("email", "N/A")
        ]]

        if existing_row_index != -1:
            # OVERWRITE existing row
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{month_name}'!A{existing_row_index}:E{existing_row_index}",
                valueInputOption="USER_ENTERED",
                body={'values': row_values}
            ).execute()
        else:
            # APPEND new row
            service.spreadsheets().values().append(
                spreadsheetId=spreadsheet_id,
                range=f"'{month_name}'!A:E",
                valueInputOption="USER_ENTERED",
                body={'values': row_values}
            ).execute()

    except Exception as e:
        print(f"Error updating sheet: {e}")