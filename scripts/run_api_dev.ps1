Set-Location "C:\Users\shivam.naik\Desktop\ET_Hormuz"

# Auto-reload server for local development.
& "c:/Users/shivam.naik/Desktop/ET_Hormuz/.venv/Scripts/python.exe" -m uvicorn api.main:app --host 127.0.0.1 --port 8001 --reload
