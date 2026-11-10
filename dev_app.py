"""Standalone urban-survey application: FACT collection, not urban development RULEs.

Start: uvicorn dev_app:app --host 0.0.0.0 --port 8001
The Seoul operational app.py is NOT imported, mounted, or started.
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, FileResponse

from development.review import register as register_survey
from survey_sources import SurveySources

BASE=Path(__file__).resolve().parent
app=FastAPI(title="Urban Survey FACT",version="0.1.0")
register_survey(app,SurveySources())


@app.get('/',response_class=HTMLResponse)
@app.get('/survey',response_class=HTMLResponse)
def survey_home():
    return (BASE/'survey.html').read_text(encoding='utf-8')


@app.get('/survey-ui.js')
def survey_script():
    return FileResponse(BASE/'survey-ui.js',media_type='text/javascript')


@app.get('/dev-assets/style.css')
def survey_style():
    return FileResponse(BASE/'style.css',media_type='text/css')


# Legacy /seoul and /nonurban development decision pages intentionally omitted.
# Those business workflows are not part of the independent survey application.
