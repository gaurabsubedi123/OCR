"""The web interface, driven through Flask's test client.

No browser is involved: every page is checked through the JSON its JavaScript
would call, plus a check that each HTML route renders at all.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from conftest import needs_tesseract

from ocrtool.web.app import create_app, registry


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(default_output=str(tmp_path / "output"))
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


def _wait_for(client, run_id: str, timeout: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = client.get(f"/api/runs/{run_id}").get_json()
        if snapshot["status"] in {"done", "failed", "cancelled"}:
            return snapshot
        time.sleep(0.25)
    raise AssertionError(f"run {run_id} did not finish within {timeout}s")


def test_the_start_page_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"OCR a folder of documents" in response.data


def test_health_reports_what_is_installed(client):
    health = client.get("/api/health").get_json()
    assert health["version"]
    assert "default_workers" in health


def test_browsing_folders(client, sample_folder: Path):
    data = client.get(f"/api/browse?path={sample_folder}").get_json()
    assert data["path"] == str(sample_folder)
    assert {folder["name"] for folder in data["folders"]} == {"sub"}
    assert data["parent"]


def test_browsing_somewhere_that_does_not_exist_falls_back(client):
    data = client.get("/api/browse?path=/definitely/not/here").get_json()
    assert data["path"]  # a folder is always returned, never an error page


def test_the_folder_preview_counts_what_a_run_would_read(client, sample_folder: Path):
    data = client.get(f"/api/preview-folder?path={sample_folder}").get_json()
    assert data["files"] == 2
    assert data["pages"] == 3
    assert len(data["sample"]) == 2


def test_the_folder_preview_rejects_a_file(client, sample_folder: Path):
    response = client.get(f"/api/preview-folder?path={sample_folder / 'scan.pdf'}")
    assert response.status_code == 400


def test_a_run_needs_a_real_input_folder(client, tmp_path: Path):
    response = client.post("/api/runs", json={"input_dir": "/nope", "output_dir": str(tmp_path)})
    assert response.status_code == 400
    assert "not a folder" in response.get_json()["error"]


def test_the_input_folder_may_not_sit_inside_the_output_folder(client, sample_folder: Path):
    response = client.post(
        "/api/runs",
        json={"input_dir": str(sample_folder), "output_dir": str(sample_folder.parent)},
    )
    assert response.status_code == 400
    assert "separate folders" in response.get_json()["error"]


def test_an_unknown_run_is_not_found(client):
    assert client.get("/api/runs/19990101-000000").status_code == 404
    assert client.get("/runs/19990101-000000").status_code == 404


@needs_tesseract
def test_a_run_started_from_the_browser_produces_everything_the_ui_needs(
    client, sample_folder: Path, tmp_path: Path
):
    output = tmp_path / "output"
    created = client.post(
        "/api/runs",
        json={"input_dir": str(sample_folder), "output_dir": str(output), "dpi": 150, "workers": 2},
    )
    assert created.status_code == 201
    run_id = created.get_json()["run_id"]

    snapshot = _wait_for(client, run_id)
    assert snapshot["status"] == "done"
    assert snapshot["totals"]["pages_done"] == 3
    assert len(snapshot["files"]) == 2

    # The run page and the document page both render.
    assert client.get(f"/runs/{run_id}").status_code == 200
    assert client.get(f"/runs/{run_id}/documents/0").status_code == 200

    document = client.get(f"/api/runs/{run_id}/documents/0").get_json()
    assert document["status"] == "done"
    assert document["pages"][0]["text"]
    assert document["pages"][0]["preview"].startswith("_previews/")

    # Every file the viewer links to is actually served.
    for relpath in document["outputs"].values():
        assert client.get(f"/files/{run_id}/{relpath}").status_code == 200
    assert client.get(f"/files/{run_id}/{document['pages'][0]['preview']}").status_code == 200
    assert client.get(f"/files/{run_id}/_runs/{run_id}/pages.csv?download=1").status_code == 200

    # Search finds a word that was only ever on a scanned page.
    hits = client.get(f"/api/runs/{run_id}/search?q=MARTINEZ").get_json()["hits"]
    assert hits and hits[0]["page"] == 1
    assert "MARTINEZ" == hits[0]["snippet"]["match"]

    # And the run is listed for next time.
    assert any(run["run_id"] == run_id for run in client.get("/api/runs").get_json()["runs"])


@needs_tesseract
def test_uploaded_files_are_staged_and_can_be_read(client, sample_folder: Path, tmp_path: Path):
    output = tmp_path / "output"
    with (sample_folder / "scan.pdf").open("rb") as fh:
        uploaded = client.post(
            "/api/upload",
            data={"output_dir": str(output), "files": (fh, "Ex 13/scan.pdf")},
            content_type="multipart/form-data",
        )
    assert uploaded.status_code == 200
    staged = Path(uploaded.get_json()["path"])
    # The folder structure the browser reported is preserved.
    assert (staged / "Ex 13" / "scan.pdf").is_file()

    created = client.post(
        "/api/runs",
        json={"input_dir": str(staged), "output_dir": str(output), "dpi": 150, "workers": 2},
    )
    run_id = created.get_json()["run_id"]
    assert _wait_for(client, run_id)["totals"]["pages_done"] == 2


@needs_tesseract
def test_the_browser_reports_documents_it_did_not_need_to_read(client, sample_folder: Path, tmp_path: Path):
    output = tmp_path / "output"
    body = {"input_dir": str(sample_folder), "output_dir": str(output), "dpi": 150, "workers": 2}

    first = client.post("/api/runs", json=body).get_json()["run_id"]
    _wait_for(client, first)

    second = client.post("/api/runs", json=body).get_json()["run_id"]
    snapshot = _wait_for(client, second)

    assert snapshot["totals"]["files_skipped"] == 2
    assert snapshot["totals"]["pages_done"] == 0
    assert all(f["status"] == "skipped" for f in snapshot["files"])
    # The skipped document still opens, with the earlier run's text in it.
    document = client.get(f"/api/runs/{second}/documents/0").get_json()
    assert "MARTINEZ" in document["pages"][0]["text"]


def test_an_upload_cannot_escape_the_staging_folder(client, tmp_path: Path):
    output = tmp_path / "output"
    uploaded = client.post(
        "/api/upload",
        data={
            "output_dir": str(output),
            "files": (Path(__file__).open("rb"), "../../escaped.pdf"),
        },
        content_type="multipart/form-data",
    )
    staged = Path(uploaded.get_json()["path"])
    assert (staged / "escaped.pdf").is_file()
    assert not (output.parent / "escaped.pdf").exists()


@needs_tesseract
def test_cancelling_a_run_from_the_browser(client, sample_folder: Path, tmp_path: Path):
    created = client.post(
        "/api/runs",
        json={"input_dir": str(sample_folder), "output_dir": str(tmp_path / "out"), "dpi": 150, "workers": 1},
    )
    run_id = created.get_json()["run_id"]
    assert client.post(f"/api/runs/{run_id}/cancel").get_json()["ok"] is True

    snapshot = _wait_for(client, run_id)
    assert snapshot["status"] in {"cancelled", "done"}  # a tiny folder can beat the click
    registry.get(run_id).join(timeout=30)
