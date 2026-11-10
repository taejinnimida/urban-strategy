"""Build a redistributable urban-survey archive, excluding ALL Seoul code.

Run from repo root: python standalone/build.py --out urban-survey-standalone.zip
The archive can be copied to another repository/server without the Seoul app.
No local credentials, user data, backups, or hidden files are bundled.
"""
from argparse import ArgumentParser
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]
EXPLICIT=("dev_app.py","survey_sources.py","survey.html","survey-ui.js","style.css")
STANDALONE=("requirements.txt","Dockerfile",".dockerignore",".env.example","README.md")
REQUIRED_PACKAGES=("development","facts")
DENIED_ROOT={
    "app.py","app.html","dev_app.html","market_renewal.py",
    "seoul_facilities.py","ui_editor_backend.py","development-ui.js",
    "index.html","nonurban.html","nonurban.js",
}


def bundle_paths():
    chosen=[ROOT/name for name in EXPLICIT]
    chosen.extend(ROOT/"standalone"/name for name in STANDALONE)
    for module in REQUIRED_PACKAGES:
        chosen.extend(sorted((ROOT/module).rglob("*.py")))
    for path in chosen:
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.name in DENIED_ROOT:
            raise ValueError("Seoul runtime forbidden in bundle")
    return chosen


def build(path):
    path=Path(path).resolve()
    with zipfile.ZipFile(path,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=8) as package:
        for source in bundle_paths():
            if source.parent==ROOT/"standalone":
                name=source.name
            else:
                name=source.relative_to(ROOT).as_posix()
            package.write(source,name)
        members=set(package.namelist())
        assert "dev_app.py" in members and "facts/vworld.py" in members
        assert "development/review.py" in members and "Dockerfile" in members
        assert not members.intersection(DENIED_ROOT)
        assert not any(p.startswith(("tests/","national_data/","rollback/")) for p in members)
    return path


if __name__=="__main__":
    parser=ArgumentParser()
    parser.add_argument("--out",default="urban-survey-standalone.zip")
    opts=parser.parse_args()
    print(build(opts.out))
