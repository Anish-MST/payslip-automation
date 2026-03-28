import pdfplumber
import re

def extract_employee_name(pdf_path):
    # Adjusted patterns based on your specific PDF OCR structure
    patterns = {
        "Employee Name": r"Employee Name\s+(.*?)\s+Employee ID",
        "Employee ID": r"Employee ID\s+(\d+)",
        "Designation": r"Designation\s+(.*?)\s+Bank Account",
        "PAN": r"PAN Number\s+([A-Z]{5}[0-9]{4}[A-Z]{1})",
        "Bank Account": r"Bank Account\s+(\d+)",
        "UAN": r"UAN\s+(\d+)",
        "Month": r"Payslip for the Month\s+(.*)"
    }
    
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

        # Clean up the text: replace multiple spaces with a single space to help matching
        text = re.sub(r' +', ' ', text)

        for field, regex in patterns.items():
            match = re.search(regex, text, re.IGNORECASE)
            if match:
                extracted_data[field] = match.group(1).strip()
            else:
                extracted_data[field] = "NOT_FOUND"
            
        return extracted_data

    except Exception as e:
        print(f"   - Error parsing PDF {pdf_path}: {e}")
        return None