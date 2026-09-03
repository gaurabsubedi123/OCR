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
    data = client.post("/api/preview-folder", json={"path": str(sample_folder)}).get_json()
    assert data["files"] == 2
    assert data["pages"] == 3


def test_the_folder_preview_returns_the_folder_tree(client, sample_folder: Path):
    """The shape of the folder is what a case file is organised by.

    sample_folder holds scan.pdf at the top and sub/page.png one down, so a
    correct tree has one document at the root and one folder carrying the
    other — not two paths in a list.
    """
    tree = client.post("/api/preview-folder", json={"path": str(sample_folder)}).get_json()["tree"]
    assert [d["name"] for d in tree["documents"]] == ["scan.pdf"]
    assert [f["name"] for f in tree["folders"]] == ["sub"]

    sub = tree["folders"][0]
    assert sub["files"] == 1 and sub["pages"] == 1
    assert [d["name"] for d in sub["documents"]] == ["page.png"]
    # The root's counts include everything beneath it, not just its own files.
    assert tree["files"] == 2 and tree["pages"] == 3


def test_the_preview_says_which_documents_are_new(client, sample_folder: Path, tmp_path: Path):
    """Adding one file to a read folder has to show that one file."""
    output = tmp_path / "out"
    body = {"path": str(sample_folder), "output_dir": str(output)}

    first = client.post("/api/preview-folder", json=body).get_json()
    assert first["new"] == 2 and first["done"] == 0
    assert all(d["state"] == "new" for d in first["tree"]["documents"])


def test_the_folder_preview_rejects_a_file(client, sample_folder: Path):
    response = client.post(
        "/api/preview-folder", json={"path": str(sample_folder / "scan.pdf")}
    )
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


@needs_tesseract
def test_the_viewer_works_with_the_tools_folders_kept_apart(
    client, sample_folder: Path, tmp_path: Path
):
    """With a work folder chosen, results and page pictures live in different
    folders — and the viewer has to serve both without being told which."""
    output = tmp_path / "output"
    work = tmp_path / "work"
    created = client.post(
        "/api/runs",
        json={
            "input_dir": str(sample_folder),
            "output_dir": str(output),
            "work_dir": str(work),
            "dpi": 150,
            "workers": 2,
        },
    )
    assert created.status_code == 201
    run_id = created.get_json()["run_id"]

    snapshot = _wait_for(client, run_id)
    assert snapshot["status"] == "done"
    assert snapshot["work_dir"] == str(work)

    # The output folder holds the results and nothing else.
    assert sorted(p.name for p in output.iterdir()) == ["json", "pdf", "sub", "txt"]

    assert client.get(f"/runs/{run_id}").status_code == 200
    document = client.get(f"/api/runs/{run_id}/documents/0").get_json()
    assert document["pages"][0]["text"]

    # A result, served out of the output folder.
    for relpath in document["outputs"].values():
        assert client.get(f"/files/{run_id}/{relpath}").status_code == 200
    # A page picture and a report, served out of the work folder.
    assert client.get(f"/files/{run_id}/{document['pages'][0]['preview']}").status_code == 200
    assert client.get(f"/files/{run_id}/_runs/{run_id}/pages.csv?download=1").status_code == 200

    hits = client.get(f"/api/runs/{run_id}/search?q=MARTINEZ").get_json()["hits"]
    assert hits


@needs_tesseract
def test_the_input_folder_may_not_sit_inside_the_work_folder(client, sample_folder: Path):
    """The work folder holds a PDF and a JPEG of every page, so reading from
    inside it is the same feedback loop wearing a different hat."""
    response = client.post(
        "/api/runs",
        json={
            "input_dir": str(sample_folder),
            "output_dir": str(sample_folder.parent / "out"),
            "work_dir": str(sample_folder.parent),
        },
    )
    assert response.status_code == 400
    assert "work folder" in response.get_json()["error"]


@needs_tesseract
def test_the_preview_marks_what_has_already_been_read(client, sample_folder: Path, tmp_path: Path):
    """Read the folder, add a file, and the preview names the one new file."""
    output = tmp_path / "out"
    body = {"path": str(sample_folder), "output_dir": str(output)}

    started = client.post(
        "/api/runs", json={"input_dir": str(sample_folder), "output_dir": str(output)}
    ).get_json()
    run = registry.get(started["run_id"])
    run.join(timeout=180)

    after = client.post("/api/preview-folder", json=body).get_json()
    assert after["done"] == 2 and after["new"] == 0
    assert all(d["state"] == "done" for d in after["tree"]["documents"])
    assert after["tree"]["documents"][0]["done_in"] == started["run_id"]

    from conftest import text_page

    text_page(["A DOCUMENT ADDED LATER"]).save(sample_folder / "3. added.png")
    later = client.post("/api/preview-folder", json=body).get_json()
    assert later["new"] == 1 and later["done"] == 2
    added = [d for d in later["tree"]["documents"] if d["name"] == "3. added.png"]
    assert added and added[0]["state"] == "new"


def test_a_run_whose_process_died_is_not_reported_as_still_running():
    """A manifest is written as the run goes, so one that was killed part-way
    is left saying `running` for ever.

    Read back by a process that is not executing it, that is not a claim that
    can be true — and the browser showed a progress bar that never moved and a
    Stop button that could not do anything. Seen for real: a server killed
    mid-run left the run listed as running in every later session.
    """
    from ocrtool.web.state import Registry

    registry = Registry()
    for stuck in ("running", "discovering", "pending"):
        seen = registry.as_read_from_disk({"run_id": "x", "status": stuck})
        assert seen["status"] == "interrupted"
        assert seen["live"] is False

    # A run that reached an end of its own keeps the end it reached.
    for settled in ("done", "failed", "cancelled"):
        assert registry.as_read_from_disk({"status": settled})["status"] == settled


def test_reading_a_dead_run_back_does_not_alter_what_is_on_disk():
    """The manifest is the record of what happened; only the reading changes."""
    from ocrtool.web.state import Registry

    manifest = {"run_id": "x", "status": "running", "totals": {"pages_done": 7}}
    seen = Registry().as_read_from_disk(manifest)
    assert manifest["status"] == "running", "the caller's dict was mutated"
    assert seen["totals"]["pages_done"] == 7
