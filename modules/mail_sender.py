import os
import base64
from email.message import EmailMessage
from googleapiclient.discovery import build

def send_employee_mail(employee_email, pdf_path, emp_name, month, creds):
    """
    Sends a concise, professional email to the employee.
    """
    try:
        service = build('gmail', 'v1', credentials=creds)

        msg = EmailMessage()
        
        # --- CLEAN SUBJECT ---
        msg['Subject'] = f"Payslip: {month} - {emp_name}"
        msg['To'] = employee_email
        
        # --- CONCISE BODY CONTENT ---
        body_content = f"""Dear {emp_name},

Please find your payslip for {month} attached. 

For any queries, please contact Jamuna at jamuna@mainstreamtek.com.

Regards,
HR Operations | MainstreamTek"""

        msg.set_content(body_content)

        # Attach PDF
        file_name = os.path.basename(pdf_path)
        if os.path.exists(pdf_path):
            with open(pdf_path, 'rb') as f:
                pdf_data = f.read()
                msg.add_attachment(
                    pdf_data, 
                    maintype='application', 
                    subtype='pdf', 
                    filename=file_name
                )
        else:
            print(f"   - Attachment error: File not found at {pdf_path}")
            return False

        # Base64 Encoding for Gmail API
        encoded_message = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')
        
        # Send
        service.users().messages().send(
            userId="me", 
            body={'raw': encoded_message}
        ).execute()
        
        return True
        
    except Exception as e:
        print(f"   - Failed to send mail to {emp_name} ({employee_email}): {e}")
        return False