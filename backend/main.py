from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# --- CORS so GitHub Pages can call backend ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://vikesh2608.github.io"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Health check route ---
@app.get("/health")
def health():
    return {"ok": True}

# --- Officials endpoint (dummy data to verify plumbing) ---
@app.get("/officials")
def get_officials(zip: str):
    return {
        "zip": zip,
        "officials": [
            {
                "name": "John Doe",
                "office": "Mayor",
                "party": "Independent",
                "phones": ["123-456-7890"],
                "emails": ["mayor@example.com"],
                "urls": ["https://example.com"],
            }
        ],
    }

