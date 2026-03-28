def validate_employee(extracted_data, master_df):
    # 1. Find the employee in Master Sheet by Employee ID
    emp_id = extracted_data.get("Employee ID")
    
    # Ensure ID comparison is string-to-string
    row = master_df[master_df['Employee ID'].astype(str) == str(emp_id)]
    
    if row.empty:
        return {"status": "FAIL", "reason": f"Employee ID {emp_id} not found in Master Sheet"}

    master_row = row.iloc[0]
    errors = []
    
    # 2. Check the 7 fields (Map PDF keys to Master Sheet Column names)
    # PDF Key : Master Sheet Column Name
    check_map = {
        "Employee Name": "Employee Name",
        "Designation": "Designation",
        "PAN": "PAN",
        "Bank Account": "Bank Account",
        "UAN": "UAN",
        "Month": "Month"
    }
    
    for pdf_key, excel_col in check_map.items():
        pdf_val = str(extracted_data.get(pdf_key, "")).strip().lower()
        master_val = str(master_row.get(excel_col, "")).strip().lower()
        
        # Special handling for Month if format differs (e.g., "February 2026" vs "Feb-26")
        if pdf_key == "Month":
            # Just a loose check for month if needed, or skip strict validation
            if not master_val[:3].lower() in pdf_val.lower():
                errors.append(f"Month Mismatch (PDF: {pdf_val} vs Master: {master_val})")
            continue

        if pdf_val != master_val:
            errors.append(f"{pdf_key} Mismatch (PDF: {pdf_val} vs Master: {master_val})")

    # Get email from the correct column "Mail Id"
    recipient_email = master_row.get("Mail Id")

    if errors:
        return {
            "status": "FAIL", 
            "reason": "; ".join(errors),
            "email": recipient_email
        }
    
    return {"status": "PASS", "email": recipient_email}