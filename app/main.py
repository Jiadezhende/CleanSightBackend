from fastapi import FastAPI
from app.routers.inspection import router as inspection_router

app = FastAPI(
    title="CleanSight Backend",
    description="AI-powered inspection of the endoscope cleaning process at Changhai Hospital",
    version="1.0.0"
)

app.include_router(inspection_router)

@app.get("/")
async def root():
    return {"message": "Welcome to CleanSight Backend"}

@app.get("/health")
async def health_check():
    return {"status": "healthy"}