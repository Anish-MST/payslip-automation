import pdfplumber
import re

def extract_employee_name(pdf_path):
    # 1. Broad labels to find specific fields
    patterns = {
        "Employee Name": r"Employee Name\s+(.*?)\s+Employee ID",
        "Employee ID": r"Employee ID\s+(\d+)",
        "Designation": r"Designation\s+(.*?)\s+Bank Account",
        "PAN": r"PAN\s*(?:Number|No)?\s*[:\-]?\s*([A-Z]{5}[0-9]{4}[A-Z]{1})",
        "Bank Account": r"Bank Account\s+(\d+)",
        "UAN": r"UAN\s+(\d+)",
        "Month": r"Payslip for the Month\s+(.*)"
    }
    
    # 2. Backup pattern for PAN (Search anywhere in the text if label fails)
    global_pan_pattern = r"([A-Z]{5}[0-9]{4}[A-Z]{1})"
    
    extracted_data = {}
    
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = ""
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"

        if not text:
            return None

        # Clean up text for better matching
        clean_text = re.sub(r' +', ' ', text)

        # First pass: try to find everything using the labels
        for field, regex in patterns.items():
            match = re.search(regex, clean_text, re.IGNORECASE)
            if match:
                extracted_data[field] = match.group(1).strip()
            else:
                extracted_data[field] = "NOT_FOUND"

        # SECOND PASS: If PAN is "NOT_FOUND", look for any 10-char PAN string anywhere
        if extracted_data["PAN"] == "NOT_FOUND":
            all_pans = re.findall(global_pan_pattern, clean_text)
            if all_pans:
                # Use the first valid PAN found in the document
                extracted_data["PAN"] = all_pans[0]

        return extracted_data

    except Exception as e:
        print(f"   - Error parsing PDF {pdf_path}: {e}")
        return None