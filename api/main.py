from fastapi import FastAPI

app = FastAPI(title="Market Data Pipeline API")

@app.get("/health")
def health():
    return {"status": "ok"}


